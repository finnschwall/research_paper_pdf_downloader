"""Last-resort source: the publisher's own article page.

Every other provider asks an open-access aggregator "is there a free copy of this?". When
they all say no, the pipeline used to stop and report `unresolved_no_legal_pdf` -- even
though the server it runs on may be entitled to the article through its institution's
subscription. On a university network that is the single largest recoverable failure mode
left: a measured 5 of 110 missing papers in one review were subscription articles that
download on the first try, and none of them was ever asked for.

Where the URL comes from: Crossref's ``resource.primary.URL``, which is the DOI's
registered destination. Three reasons not to just hand the download stage
``https://doi.org/<doi>``:

* ``host_gate.hold`` is re-entrant per thread, so a redirect chain is rate-limited under
  the *first* host only. A doi.org URL would fetch the publisher at doi.org's rate rather
  than the publisher's own, which is how a ban gets earned.
* ``PDFDownloader._request_headers`` picks its User-Agent from the requested host, and
  several publishers answer the default agent with a 403.
* The candidate's domain is what ends up in ``PaperDocument.source_url``, so "doi.org" is
  worse provenance than the page we actually read.

Crossref's ``link[]`` array is deliberately not used here -- ``CrossrefSourceProvider``
already mines it, and for IEEE it holds a ``xplorestaging.ieee.org`` host that does not
serve anyone. ``resource.primary.URL`` is correct for the same DOIs.

The candidate is marked ``fallback_only``, so it sorts below every other candidate and
never ends the provider search: a free copy on arXiv is always the better fetch, both
because it costs a publisher nothing and because publisher hosts are the ones behind bot
walls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import requests

from paper_downloader.config.models import ApiConfig, DownloadConfig, ResolutionConfig
from paper_downloader.core import host_gate
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import SourceCandidate
from paper_downloader.sources.crossref import is_staging_url

#: Below every aggregator's exact-lookup confidence. A landing page is a promise that the
#: article exists somewhere on that host, not that we may read it.
_CONFIDENCE = 0.50


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        domain = (urlparse(url).netloc or "").lower().strip()
    except Exception:
        return None
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


@dataclass(slots=True)
class PublisherLandingSourceProvider:
    api_config: ApiConfig
    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    base_url: str = "https://api.crossref.org"
    name: str = "publisher_landing"
    _session: requests.Session = field(default=None, init=False, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._session = host_gate.GatedSession()
        self._session.headers.update(self._headers())

    def __del__(self) -> None:
        if self._session is not None:
            self._session.close()

    def resolve(self, paper: PaperRecord) -> list[SourceCandidate]:
        if not paper.doi:
            return []

        url = self._primary_url(paper.doi)
        if not url or is_staging_url(url):
            return []
        if not url.lower().startswith(("http://", "https://")):
            return []

        is_direct_pdf = url.lower().endswith(".pdf")
        return [
            SourceCandidate(
                source_name=self.name,
                pdf_url=url,
                landing_page_url=url,
                # Honest: this *is* the version of record. It sorts last anyway, because
                # `fallback_only` outranks every other term in the resolver's sort key.
                version_type="publisher",
                host_type="publisher",
                domain=_domain_from_url(url),
                confidence=_CONFIDENCE,
                is_direct_pdf=is_direct_pdf,
                fallback_only=True,
                reason="crossref resource.primary url (publisher article page)",
                metadata={"doi": paper.doi},
            )
        ]

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.download_config.user_agent,
        }
        if self.api_config.crossref_email:
            headers["User-Agent"] = (
                f"{self.download_config.user_agent} (mailto:{self.api_config.crossref_email})"
            )
        return headers

    def _timeout(self) -> tuple[int, int]:
        return (
            self.download_config.connect_timeout_seconds,
            self.download_config.read_timeout_seconds,
        )

    def _primary_url(self, doi: str) -> str | None:
        """The DOI's registered destination, or None if Crossref does not know it.

        Every failure here is a None: a paper with no free copy and no reachable Crossref
        record is unavailable either way, and raising would only cost the provider chain
        its remaining candidates.
        """
        encoded = quote(doi.strip(), safe="")
        try:
            response = self._session.get(
                f"{self.base_url}/works/{encoded}",
                timeout=self._timeout(),
                allow_redirects=True,
                verify=self.download_config.verify_ssl,
            )
        except requests.RequestException:
            return None

        if response.status_code >= 400:
            return None

        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None

        message = payload.get("message")
        if not isinstance(message, dict):
            return None

        resource: Any = message.get("resource") or {}
        if not isinstance(resource, dict):
            return None
        primary = resource.get("primary") or {}
        if not isinstance(primary, dict):
            return None

        url = primary.get("URL")
        return url.strip() if isinstance(url, str) and url.strip() else None
