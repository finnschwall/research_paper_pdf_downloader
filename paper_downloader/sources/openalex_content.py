"""OpenAlex's own copy of the PDF, for papers no publisher will serve to a script.

OpenAlex caches the full text of the open-access works it indexes -- more than 50 million of
them -- and serves the file from its own host. That matters because the papers this pipeline
cannot get are rarely paywalled: they are open-access articles on publisher sites whose edge
network refuses any client that is not a browser. MDPI, ACM and IOP articles that are free to
read under CC-BY are simply unreachable over HTTP, from any address. OpenAlex has them.

Two things make this a separate provider rather than a few lines inside ``sources/openalex.py``:

* **It costs money.** About a cent per file, against a free daily allowance of roughly a
  hundred. Two separate mechanisms keep it from being the first thing tried, and both are
  needed. Its place late in ``resolution.source_priority`` means the provider is usually not
  even *asked*: the resolver stops at the first good-enough candidate and only reaches the
  rest once everything found so far has failed to download. And its candidate is
  ``fallback_only``, which means that even when the provider *is* asked -- which is exactly
  the case for a paper whose only other copies are on walled hosts -- the candidate sorts
  below every ordinary one, so a free repository copy is still downloaded first. Without the
  second, an MDPI paper with a free Europe PMC copy would have been paid for: nothing before
  it was "good enough" to stop the chain, and a publisher-version candidate outranks an
  accepted manuscript on every other term in the sort key.
* **It needs the API key**, and answers 401 without one. A deployment with no key gets a
  provider that reports why it did nothing and asks nobody.

The key goes on the request, never in the URL: see ``paper_downloader.core.credentials``.
A manifest that recorded ``?api_key=`` would publish the key to everyone who can read a run's
output, and in SEER that is every logged-in account.

The licence on a cached PDF is the article's own. OpenAlex grants no additional rights, which
is why this provider only offers files the work record marks as open access.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

from paper_downloader.config.models import ApiConfig, DownloadConfig, ResolutionConfig
from paper_downloader.core import host_gate
from paper_downloader.core.credentials import OPENALEX_CONTENT_HOST
from paper_downloader.metadata import works
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import SourceCandidate

#: High: this is a direct link to a file on a host that has never refused us, and the work
#: record has already said the file is there. It ranks below a publisher PDF only because it
#: is asked later, not because it is worse.
_CONFIDENCE = 0.88


@dataclass(slots=True)
class OpenAlexContentSourceProvider:
    api_config: ApiConfig
    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    base_url: str = "https://api.openalex.org"
    name: str = "openalex_content"
    _session: requests.Session = field(default=None, init=False, repr=False)  # type: ignore[assignment]
    last_reason: str | None = field(default=None, init=False, repr=False)
    last_failed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._session = host_gate.GatedSession()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": self.download_config.user_agent,
        })

    def __del__(self) -> None:
        if self._session is not None:
            self._session.close()

    def resolve(self, paper: PaperRecord) -> list[SourceCandidate]:
        self.last_reason = None
        self.last_failed = False

        if not (self.api_config.openalex_api_key or "").strip():
            self.last_reason = "no OPENALEX_API_KEY; the content endpoint answers 401 without one"
            return []
        if not paper.doi:
            self.last_reason = "no doi to look the work up by"
            return []

        lookup = works.openalex_work(
            paper.doi,
            session=self._session,
            timeout=(
                self.download_config.connect_timeout_seconds,
                self.download_config.read_timeout_seconds,
            ),
            verify=self.download_config.verify_ssl,
            api_key=self.api_config.openalex_api_key,
            base_url=self.base_url,
        )
        if lookup.error:
            self.last_failed = True
            self.last_reason = lookup.error
            return []
        if not lookup.work:
            self.last_reason = "doi unknown to openalex"
            return []

        url = self._content_pdf_url(lookup.work)
        if not url:
            self.last_reason = "openalex holds no cached pdf for this work"
            return []

        return [
            SourceCandidate(
                source_name=self.name,
                pdf_url=url,
                landing_page_url=lookup.work.get("doi") or None,
                version_type="publisher",
                host_type="repository",
                license=(lookup.work.get("best_oa_location") or {}).get("license")
                if isinstance(lookup.work.get("best_oa_location"), dict) else None,
                domain=OPENALEX_CONTENT_HOST,
                confidence=_CONFIDENCE,
                is_direct_pdf=True,
                asserts_open_access=True,
                # Try every free copy first, whatever this one scores. It still sorts above
                # `publisher_landing`, the other fallback_only candidate, because it is a
                # direct link to a file and that one is an article page.
                fallback_only=True,
                reason="openalex full-text cache (metered, ~$0.01)",
                metadata={
                    "openalex_id": lookup.work.get("id"),
                    "oa_status": (lookup.work.get("open_access") or {}).get("oa_status")
                    if isinstance(lookup.work.get("open_access"), dict) else None,
                    "metered": True,
                },
            )
        ]

    @staticmethod
    def _content_pdf_url(work: dict[str, Any]) -> str | None:
        """The cached-PDF URL this work advertises, without any credential on it.

        ``has_content.pdf`` is the flag and ``content_urls.pdf`` is the address; a work with
        the flag and no address is a record we do not understand, so it yields nothing rather
        than a guessed URL.
        """
        has_content = work.get("has_content")
        if not (isinstance(has_content, dict) and has_content.get("pdf")):
            return None
        content_urls = work.get("content_urls")
        if not isinstance(content_urls, dict):
            return None
        url = content_urls.get("pdf")
        if not isinstance(url, str) or not url.lower().startswith("https://"):
            return None
        # Strip anything already on the query string: the credential is added at request
        # time, and a key baked into a URL is exactly what this provider must not store.
        return url.split("?", 1)[0]
