from __future__ import annotations

import contextlib
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from paper_downloader.config.models import ApiConfig, DownloadConfig
from paper_downloader.core import host_gate
from paper_downloader.core.credentials import CredentialTable, build_credentials
from paper_downloader.core.exceptions import (
    DownloadError,
    HostBlockedError,
    HTTPStatusError,
    NotAPDFError,
    WrongPaperError,
    PDFValidationError,
)
from paper_downloader.download.entitlement import not_the_full_article
from paper_downloader.download.identity import ExpectedIdentity, verify_pdf_identity
from paper_downloader.storage.writers import ensure_parent_dir

logger = logging.getLogger("paper_downloader")

# Statuses worth trying again: the server is overloaded, rate limiting, or briefly broken.
# 401/403/404 are answers, not glitches -- retrying them only wastes time and looks worse
# to the publisher.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504, 507, 509})

#: How much of an error response to read before deciding whether it is a refusal page.
_ERROR_BODY_PEEK_BYTES = 64 * 1024


def _retry_after_seconds(response: requests.Response) -> float:
    """`Retry-After` in seconds, or a conservative default when it is absent or a date.

    Only the delta-seconds form is parsed. The HTTP-date form is rare here and guessing at
    clock skew to save a few seconds of waiting is not worth the code.
    """
    raw = (response.headers.get("Retry-After") or "").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


@dataclass(slots=True)
class DownloadResult:
    url: str
    final_url: str
    output_path: str
    content_type: str | None
    size_bytes: int
    sha256: str
    reused_existing: bool = False
    #: What the identity check concluded: state, reason and the signal that decided it.
    #: None when the check was off or the caller had nothing to check against.
    identity: dict | None = None

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "output_path": self.output_path,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "reused_existing": self.reused_existing,
            "identity": self.identity,
        }


class PDFDownloader:
    """Fetches PDFs over one long-lived HTTP session.

    The session is deliberately not per-attempt. A gold-OA DOI usually resolves to an
    article page, so the real fetch is two requests -- landing page, then the PDF link on
    it -- and several platforms set a cookie on the first that the second must present.
    Nature answers a cookie-less PDF request with `?error=cookies_not_supported` and serves
    the article HTML instead, which used to be recorded as "landing page had no PDF". Not
    thread-safe, for the same reason `requests.Session` is not: give each thread its own.
    """

    def __init__(
        self,
        config: DownloadConfig,
        *,
        api_config: ApiConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self._session = session if session is not None else host_gate.GatedSession()
        self._owns_session = session is None
        # Host -> credential. Attached at request time and never at URL-building time, so
        # nothing secret reaches a manifest, a stats file or a stored source URL. See
        # paper_downloader.core.credentials.
        self.credentials: CredentialTable = build_credentials(api_config)

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "PDFDownloader":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def download(
        self,
        url: str,
        output_path: str | Path,
        *,
        skip_if_valid: bool = True,
        referer: str | None = None,
        expected: ExpectedIdentity | None = None,
    ) -> DownloadResult:
        """Fetch `url` into `output_path`, retrying only what is worth retrying.

        `referer` is sent when we arrived at this URL from a landing page; several
        platforms serve the PDF only to requests that look like they came from the
        article page.

        `expected` is what the caller knows about the paper it asked for. When given, the
        downloaded PDF's front pages are checked for it and a file that is legibly a
        different document raises WrongPaperError instead of being kept.
        """
        target_path = Path(output_path)

        if skip_if_valid and target_path.exists():
            size_bytes = target_path.stat().st_size
            self._validate_existing_file(target_path, size_bytes=size_bytes)
            return DownloadResult(
                url=url,
                final_url=url,
                output_path=str(target_path),
                content_type="application/pdf",
                size_bytes=size_bytes,
                sha256=self._sha256_file(target_path),
                reused_existing=True,
            )

        host_gate.check_blocked(url)

        ensure_parent_dir(target_path)
        tmp_path = target_path.with_suffix(target_path.suffix + ".part")

        # config.max_retries counts retries, so attempt 1 is not one of them.
        attempts = max(1, self.config.max_retries + 1)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._attempt_download(url, target_path, tmp_path, referer=referer, expected=expected)
            except (NotAPDFError, PDFValidationError):
                # The server gave us something, it just was not a PDF. Asking again gets
                # the same thing back.
                raise
            except (HTTPStatusError, DownloadError) as exc:
                last_error = exc
                if not self._is_retryable(exc) or attempt == attempts:
                    raise
                delay = self.config.retry_backoff_seconds * (2 ** (attempt - 1))
                logger.info(
                    "download attempt %d/%d failed (%s) | retrying in %.1fs | %s",
                    attempt, attempts, exc, delay, url,
                )
                time.sleep(delay)

        raise last_error or DownloadError(f"Failed to download PDF from url: {url}")

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, HostBlockedError):
            # The host is turning us away at the edge. Asking four times is how the block
            # got there in the first place.
            return False
        if isinstance(exc, HTTPStatusError):
            return exc.status_code in _RETRYABLE_STATUSES
        # A bare DownloadError here means the request never completed -- DNS, connect
        # timeout, read timeout, reset connection. All worth one more try.
        return True

    def _attempt_download(
        self,
        url: str,
        target_path: Path,
        tmp_path: Path,
        *,
        referer: str | None = None,
        expected: ExpectedIdentity | None = None,
    ) -> DownloadResult:
        try:
            # Headers go per-request, not onto the session: the session outlives this URL
            # and `_request_headers` is domain-specific, so mutating it here would send one
            # publisher's browser disguise to the next one.
            with contextlib.closing(
                self._get(url, referer=referer)
            ) as response:
                if response.status_code >= 400:
                    self._raise_for_error_status(url, response)

                content_type = response.headers.get("Content-Type")
                final_url = str(response.url)

                # A publisher API can answer with a valid PDF that is only the article's
                # first page. Every later check passes, so the one moment this is catchable
                # is here, while the entitlement header is still in hand.
                preview = not_the_full_article(host_gate.host_key(final_url), response.headers)
                if preview:
                    raise HTTPStatusError(
                        f"{host_gate.host_key(final_url)} sent a preview, not the article "
                        f"({preview}) for url: {url}",
                        status_code=403,
                    )
                total_bytes = 0
                first_bytes = b""
                sha256 = hashlib.sha256()

                try:
                    with tmp_path.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 128):
                            if not chunk:
                                continue

                            if not first_bytes:
                                first_bytes = chunk[:32]

                            total_bytes += len(chunk)
                            if total_bytes > self.config.max_pdf_bytes:
                                raise PDFValidationError(
                                    f"Downloaded file exceeds max size limit: {self.config.max_pdf_bytes}"
                                )

                            sha256.update(chunk)
                            handle.write(chunk)
                except Exception:
                    tmp_path.unlink(missing_ok=True)
                    raise

                # An empty 2xx that is not a 200 is a bot challenge, not a document: IEEE
                # answers every article URL with a 202 and no body. Without this the file
                # merely fails the size check, and "too small to be a valid PDF" says
                # nothing about why.
                if total_bytes == 0 and response.status_code != 200:
                    tmp_path.unlink(missing_ok=True)
                    kind = host_gate.note_response(
                        url, response.status_code, b"", headers=response.headers,
                    )
                    if kind:
                        raise HostBlockedError(
                            f"{host_gate.host_key(url)} answered HTTP "
                            f"{response.status_code} with an empty body for: {url}",
                            host=host_gate.host_key(url),
                            challenge=(kind == "challenge"),
                        )
        except (DownloadError, PDFValidationError):
            raise
        except Exception as exc:
            raise DownloadError(f"Failed to download PDF from url: {url}") from exc

        try:
            self._validate_downloaded_file(
                tmp_path,
                content_type=content_type,
                first_bytes=first_bytes,
                size_bytes=total_bytes,
                final_url=final_url,
            )
        except Exception:
            # Never leave a .part behind: the next run would otherwise find stale bytes,
            # and a half-written HTML page under a .pdf name is actively confusing.
            tmp_path.unlink(missing_ok=True)
            raise

        identity = self._check_identity(tmp_path, expected, final_url=final_url)

        tmp_path.replace(target_path)

        return DownloadResult(
            url=url,
            final_url=final_url,
            output_path=str(target_path),
            content_type=content_type,
            size_bytes=total_bytes,
            sha256=sha256.hexdigest(),
            reused_existing=False,
            identity=identity,
        )

    def _check_identity(
        self, tmp_path: Path, expected: ExpectedIdentity | None, *, final_url: str,
    ) -> dict | None:
        """Is this the paper we asked for? Refuses on positive evidence only.

        Every check before this one asks "is it a PDF". This one asks "is it *this* PDF",
        which the file-type checks cannot: a document the paper cites, a first-page
        preview and a supplement under the article's DOI all pass them. A verdict that
        merely could not be reached -- no text layer, nothing to compare against -- keeps
        the file, because a wrong file kept can be found by re-running the audit and a
        right file deleted cannot.
        """
        if not self.config.verify_identity or expected is None or expected.is_empty():
            return None
        expected.source_url = final_url or expected.source_url
        verdict = verify_pdf_identity(tmp_path, expected)
        if verdict.positive_rejection:
            tmp_path.unlink(missing_ok=True)
            raise WrongPaperError(
                f"Downloaded PDF is not the requested paper: {verdict.reason}",
                verdict=verdict.to_dict(), final_url=final_url,
            )
        if not verdict.ok:
            logger.warning("keeping an unverified PDF | %s | %s", verdict.reason, final_url)
        return verdict.to_dict()

    def _get(self, url: str, *, referer: str | None) -> requests.Response:
        headers = self._request_headers(url, referer=referer)
        params: dict[str, str] | None = None
        credential = self.credentials.for_url(url)
        if credential is not None:
            headers.update(credential.headers)
            params = dict(credential.params) or None
        try:
            return self._session.get(
                url,
                headers=headers,
                params=params,
                stream=True,
                timeout=(self.config.connect_timeout_seconds, self.config.read_timeout_seconds),
                allow_redirects=True,
                verify=self.config.verify_ssl,
            )
        except requests.RequestException as exc:
            raise DownloadError(f"Failed to download PDF from url: {url}") from exc

    def _raise_for_error_status(self, url: str, response: requests.Response) -> None:
        """Turn an error status into the most specific exception the body supports.

        The body is read here -- bounded, and it was going to be discarded anyway --
        because the difference between "this article is not free" and "this site is turning
        our IP away" is only visible in it. The first is an answer about the paper; the
        second is an answer about us, and conflating them is how a temporary block became a
        permanent verdict on 36 papers.
        """
        peek = b""
        try:
            peek = next(response.iter_content(chunk_size=_ERROR_BODY_PEEK_BYTES), b"") or b""
        except Exception:
            pass

        if response.status_code == 429:
            host_gate.note_retry_after(url, _retry_after_seconds(response))

        kind = host_gate.note_response(url, response.status_code, peek, headers=response.headers)
        if kind == "challenge":
            raise HostBlockedError(
                f"{host_gate.host_key(url)} bot-challenged this client with HTTP "
                f"{response.status_code} for: {url}",
                host=host_gate.host_key(url), challenge=True,
            )
        if kind == "refusal":
            raise HostBlockedError(
                f"{host_gate.host_key(url)} refused this server with HTTP "
                f"{response.status_code} for: {url}",
                host=host_gate.host_key(url),
            )

        raise HTTPStatusError(
            f"Download returned HTTP {response.status_code} for url: {url}",
            status_code=response.status_code,
        )

    def _domain_from_url(self, url: str) -> str:
        try:
            from urllib.parse import urlparse
            netloc = urlparse(url).netloc.lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            return netloc
        except Exception:
            return ""

    def _request_headers(self, url: str, *, referer: str | None = None) -> dict[str, str]:
        """
        Return HTTP headers appropriate for the target domain.
        """
        _BROWSER_UA = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        _BROWSER_ACCEPT = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/pdf,*/*;q=0.8"
        )
        _BROWSER_DOMAINS = frozenset({
            "biorxiv.org",
            "medrxiv.org",
            "chemrxiv.org",
            "mdpi.com",
            "frontiersin.org",
            "f1000research.com",
            "peerj.com",
            "royalsocietypublishing.org",
            "plos.org",
            "journals.plos.org",
            "hindawi.com",
            "onlinelibrary.wiley.com",
            "wiley.com",
            # Added after a production run: these all answered the default
            # "paper-downloader/1.0" agent with 403 or with an HTML interstitial.
            "aacrjournals.org",
            "emerald.com",
            "dl.acm.org",
            "ieeexplore.ieee.org",
            "link.springer.com",
            "springer.com",
            "tandfonline.com",
            "sagepub.com",
            "journals.sagepub.com",
            "scitepress.org",
            "researchcommons.org",
        })

        domain = self._domain_from_url(url)
        needs_browser_ua = any(
            domain == d or domain.endswith(f".{d}")
            for d in _BROWSER_DOMAINS
        )

        if needs_browser_ua:
            headers = {
                "User-Agent": _BROWSER_UA,
                "Accept": _BROWSER_ACCEPT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            }
        else:
            headers = {
                "User-Agent": self.config.user_agent,
                "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
            }

        # Sent only when we followed a link from an article page. Several platforms serve
        # the PDF to a request that looks like it came from their own landing page and
        # refuse an otherwise identical one that does not.
        if referer:
            headers["Referer"] = referer

        return headers

    def _validate_existing_file(self, path: Path, *, size_bytes: int) -> None:
        if size_bytes < self.config.min_pdf_bytes:
            raise PDFValidationError(f"Existing PDF is too small to be valid: {path}")
        with path.open("rb") as handle:
            first_bytes = handle.read(8)
        if not self._looks_like_pdf(first_bytes):
            raise PDFValidationError(f"Existing file is not a valid PDF: {path}")

    def _validate_downloaded_file(
        self,
        path: Path,
        *,
        content_type: str | None,
        first_bytes: bytes,
        size_bytes: int,
        final_url: str = "",
    ) -> None:
        """Reject what is not the paper, saying *why* rather than "too small".

        The content check runs before the size check, and that order is the whole point.
        Imperva's interstitial is a 212-byte page whose only job is to load a JavaScript
        challenge; when the size check ran first, it raised "too small to be a valid PDF" and
        the page was never read -- so a bot wall was recorded as a broken file, and the
        obvious-looking fix (raise the size limit) would have changed nothing.
        """
        if not self._looks_like_pdf(first_bytes):
            raise NotAPDFError(
                f"Downloaded file does not look like a PDF: {path}",
                final_url=final_url,
                body=self._read_page_text(path),
            )

        if size_bytes < self.config.min_pdf_bytes:
            raise PDFValidationError(
                f"Downloaded file is too small to be a valid PDF: {path}"
            )

        if content_type:
            lowered = content_type.split(";")[0].strip().lower()
            allowed = {item.lower() for item in self.config.allowed_content_types}
            if lowered not in allowed and not lowered.endswith("/pdf"):
                raise NotAPDFError(
                    f"Server returned unexpected content type '{lowered}' for url: {path}",
                    final_url=final_url,
                    body=self._read_page_text(path),
                )

    def _read_page_text(self, path: Path) -> str:
        """Read back what the server actually sent, as text, so the caller can scan it.

        Bounded by landing_page_max_bytes -- we only need the <head> and the download
        links, and some publishers answer with megabytes of JavaScript.
        """
        if not self.config.landing_page_fallback:
            return ""
        try:
            with path.open("rb") as handle:
                raw = handle.read(self.config.landing_page_max_bytes)
        except OSError:
            return ""
        return raw.decode("utf-8", errors="replace")

    def _looks_like_pdf(self, first_bytes: bytes) -> bool:
        return first_bytes.startswith(b"%PDF-")

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 128), b""):
                digest.update(chunk)
        return digest.hexdigest()