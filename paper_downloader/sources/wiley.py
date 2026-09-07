"""Wiley's text-and-data-mining API: the sanctioned way to get a Wiley PDF.

``onlinelibrary.wiley.com`` sits behind a Cloudflare browser challenge, so no HTTP client
gets a PDF from it, from any address -- including articles published under a Creative
Commons licence. Wiley's answer to that is a separate API on a different host, with a token
you get by accepting a click-through licence on their TDM page. It works: the same articles
the website refuses come back as ``application/pdf`` in one request.

How it behaves, all of it observed rather than read off the documentation:

* The DOI goes in the path with the slash percent-encoded. An unencoded DOI answers an empty
  404, which looks exactly like "no such article".
* The response is a redirect to a single-use signed URL on ``alm.wiley.com``. Follow it, but
  record ``api.wiley.com/...`` as the source -- the signed URL expires and is useless as
  provenance.
* Missing token gives 400, not 401.
* **Entitlement is by institutional IP range as well as by token.** A token that works from a
  subscribing network answers 403 from anywhere else, and subscribed (non-open) articles come
  back only when the network is entitled to them. So a failure here says nothing about the
  article; the next provider is tried as usual.
* Rate: 3 requests per second, 60 per 10 minutes. The host gate's default (one per second) is
  already inside that.

The provider fires only for DOIs registered to Wiley, which is a prefix check and costs
nothing. Everything else returns immediately, so having it in the chain is free for the 90%
of papers that are not Wiley's.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from paper_downloader.config.models import ApiConfig, DownloadConfig, ResolutionConfig
from paper_downloader.core.credentials import WILEY_HOST
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import SourceCandidate

_API_URL = f"https://{WILEY_HOST}/onlinelibrary/tdm/v1/articles"

#: DOI prefixes Wiley registers. 10.1002 and 10.1111 are the two big ones; 10.1155 is
#: Hindawi, which Wiley acquired and whose newer content it now serves. A prefix Wiley does
#: not own would simply 404, so the list is a cost saver, not a correctness guard.
WILEY_DOI_PREFIXES: tuple[str, ...] = (
    "10.1002/", "10.1111/", "10.1155/", "10.1046/", "10.1034/", "10.1029/", "10.1113/",
)

#: The version of record, from the publisher, over a sanctioned API. Nothing outranks it
#: except a copy that costs the publisher nothing, which is why the free aggregators are
#: asked first.
_CONFIDENCE = 0.93


def is_wiley_doi(doi: str | None) -> bool:
    """Does this DOI belong to a Wiley prefix?"""
    if not doi:
        return False
    lowered = doi.strip().lower()
    return any(lowered.startswith(prefix) for prefix in WILEY_DOI_PREFIXES)


def tdm_url(doi: str) -> str:
    """The TDM endpoint for one DOI, with the slash encoded as Wiley requires."""
    return f"{_API_URL}/{quote(doi.strip(), safe='')}"


@dataclass(slots=True)
class WileySourceProvider:
    api_config: ApiConfig
    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    name: str = "wiley"
    last_reason: str | None = field(default=None, init=False, repr=False)
    last_failed: bool = field(default=False, init=False, repr=False)

    def resolve(self, paper: PaperRecord) -> list[SourceCandidate]:
        """One candidate for a Wiley DOI, nothing otherwise. Makes no request of its own."""
        self.last_reason = None
        self.last_failed = False

        if not (self.api_config.wiley_tdm_token or "").strip():
            self.last_reason = "no WILEY_TDM_TOKEN configured"
            return []
        if not paper.doi:
            self.last_reason = "no doi"
            return []
        if not is_wiley_doi(paper.doi):
            self.last_reason = f"doi {paper.doi} is not a wiley prefix"
            return []

        return [
            SourceCandidate(
                source_name=self.name,
                pdf_url=tdm_url(paper.doi),
                landing_page_url=f"https://doi.org/{paper.doi}",
                version_type="publisher",
                host_type="publisher",
                domain=WILEY_HOST,
                confidence=_CONFIDENCE,
                is_direct_pdf=True,
                # Not an open-access assertion: the API serves subscribed content to an
                # entitled network too, and claiming a free copy exists on that basis would
                # mislabel every paywalled Wiley paper as open access.
                asserts_open_access=False,
                reason="wiley text-and-data-mining api",
                metadata={"doi": paper.doi},
            )
        ]
