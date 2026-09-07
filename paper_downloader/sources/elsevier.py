"""Elsevier's Article Retrieval API: the sanctioned way past ScienceDirect's bot check.

``sciencedirect.com`` answers a non-browser client with a 403 and an 800 KB HTML page, and
``linkinghub.elsevier.com`` -- the host every Elsevier DOI redirects to -- serves a JavaScript
redirect to that same wall. So the ordinary route is closed even for open-access articles.

The API is not. A key is self-registered at dev.elsevier.com in a few minutes, and
``GET https://api.elsevier.com/content/article/doi/{doi}`` with ``Accept: application/pdf``
returns the PDF. What comes back depends on entitlement:

* Open-access article: the full PDF, for any key.
* Subscribed article, from an entitled network (or with an institutional token): the full PDF.
* Anything else: a **first page only**, or an XML error. The first page is the trap -- it is a
  valid PDF of the right article and every size and content check passes, so without a guard
  the pipeline would file a one-page teaser as the paper's full text.

The guard is ``download.entitlement.not_the_full_article``, applied by the download stage the
moment the headers arrive. It reads Elsevier's own entitlement statement
(``X-ELS-Status``/``X-ELS-ResourceVersion``) rather than trying to judge the document; when
that says the response is not the full text, the candidate fails as "not free" and the paper
is reported honestly instead of silently truncated.

Rate: no hard limit published, a "reasonable and customary" one requested. The host gate's
default of one request per second is well inside that.

**This provider is unverified.** No Elsevier key was available when it was written, so the
request shape follows Elsevier's published API and the entitlement guard is untested against
a real non-entitled response. Treat the first run with a real key as the test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from paper_downloader.config.models import ApiConfig, DownloadConfig, ResolutionConfig
from paper_downloader.core.credentials import ELSEVIER_HOST
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import SourceCandidate

_API_URL = f"https://{ELSEVIER_HOST}/content/article/doi"

#: DOI prefixes Elsevier registers. 10.1016 covers most of ScienceDirect; the others are
#: Cell Press, Lancet and the imprints Elsevier has absorbed.
ELSEVIER_DOI_PREFIXES: tuple[str, ...] = (
    "10.1016/", "10.1053/", "10.1067/", "10.1078/", "10.1006/", "10.1054/", "10.5555/",
)

_CONFIDENCE = 0.93

def is_elsevier_doi(doi: str | None) -> bool:
    if not doi:
        return False
    lowered = doi.strip().lower()
    return any(lowered.startswith(prefix) for prefix in ELSEVIER_DOI_PREFIXES)


def article_url(doi: str) -> str:
    """The Article Retrieval endpoint for one DOI."""
    return f"{_API_URL}/{quote(doi.strip(), safe='/')}"


@dataclass(slots=True)
class ElsevierSourceProvider:
    api_config: ApiConfig
    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    name: str = "elsevier"
    last_reason: str | None = field(default=None, init=False, repr=False)
    last_failed: bool = field(default=False, init=False, repr=False)

    def resolve(self, paper: PaperRecord) -> list[SourceCandidate]:
        """One candidate for an Elsevier DOI, nothing otherwise. Makes no request of its own."""
        self.last_reason = None
        self.last_failed = False

        if not (self.api_config.elsevier_api_key or "").strip():
            self.last_reason = "no ELSEVIER_API_KEY configured"
            return []
        if not paper.doi:
            self.last_reason = "no doi"
            return []
        if not is_elsevier_doi(paper.doi):
            self.last_reason = f"doi {paper.doi} is not an elsevier prefix"
            return []

        return [
            SourceCandidate(
                source_name=self.name,
                pdf_url=article_url(paper.doi),
                landing_page_url=f"https://doi.org/{paper.doi}",
                version_type="publisher",
                host_type="publisher",
                domain=ELSEVIER_HOST,
                confidence=_CONFIDENCE,
                is_direct_pdf=True,
                # Same reasoning as the Wiley provider: the API serves subscribed content
                # too, so its existence is not evidence of a free copy.
                asserts_open_access=False,
                reason="elsevier article retrieval api",
                metadata={"doi": paper.doi},
            )
        ]
