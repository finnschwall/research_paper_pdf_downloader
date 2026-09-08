"""Is this PDF the paper we asked for?

Everything upstream checks that a download *is a PDF*: magic bytes, size, content type. None
of that says it is the *right* PDF. A landing-page scrape can follow a link to a document the
paper cites; a publisher can answer with a one-page preview; a repository can hold a
supplement under the article's DOI. All three pass every file-type check and are then stored
as the paper, which in a systematic review is worse than no paper at all.

The check reads the front of the document and looks for the paper's own identifiers. Any one
match is enough -- they are alternatives, not a checklist -- because the documents that
legitimately fail one are common: an accepted manuscript carries no publisher DOI, an arXiv
preprint has no Crossref page range, a scanned page may carry no clean title.

Only ``VERIFIED`` accepts. The other states all refuse, but they are kept distinct because the
*reason* has to be honest: "this is a different paper" is a retrieval bug, "this has no text
layer" is a corpus-quality fact, "no PDF reader installed" is a broken install. The download
stage acts only on the states that are positive evidence (``WRONG``, ``TRUNCATED``); a file
that merely could not be checked is kept and marked, because a wrong file kept can be found
again by re-running the audit and a right file deleted cannot.

Method and thresholds adapted from ``fetchpdf`` (The Metascience Observatory, MIT licence),
whose measurements on biomedical corpora fixed the constants below. They were then re-checked
against this library's own corpus -- see the notes on each constant.
"""
from __future__ import annotations

import html as _html
import io
import logging
import re
import unicodedata
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Optional

from paper_downloader.metadata.id_recovery import extract_arxiv_id_from_doi

# pypdf logs one warning per malformed object in a damaged file, and publisher PDFs are
# damaged often enough that a batch run would otherwise be mostly this noise.
logging.getLogger("pypdf").setLevel(logging.ERROR)

#: Verdict states. Only the first accepts.
VERIFIED = "verified"          # positive evidence this is the requested paper
WRONG = "wrong_article"        # positive evidence it is a DIFFERENT document
TRUNCATED = "truncated"        # this article, but only a fragment of it
UNREADABLE = "unreadable"      # reader present, no usable text layer
NO_REFERENCE = "no_reference"  # nothing to compare against: no DOI, no arXiv id, no title
NO_ENGINE = "no_engine"        # pypdf is not installed

#: States on which the download stage may refuse the file. Everything else means "nobody
#: checked", which is not a statement about the file.
POSITIVE_REJECTIONS = frozenset({WRONG, TRUNCATED})

#: Pages read from the front. Enough to clear a cover sheet; few enough that a long
#: document's bibliography cannot supply the requested DOI and fake a match.
DOI_PAGES = 3
TITLE_PAGES = 2

#: How much of the title must survive on the page, summed over matching runs of at least a
#: few characters. Gap-tolerant on purpose: one substituted glyph (a Greek letter set in a
#: Latin face) must not halve the score.
TITLE_BLOCK_MIN = 0.85
#: A weaker title match that is enough only when the page count independently agrees.
TITLE_BLOCK_PARTIAL = 0.55
#: Below this the document's own embedded title is not just unmatched -- it is about
#: something else.
TITLE_BLOCK_FOREIGN = 0.30

#: Fewer squashed characters than this across the front pages and the document has not
#: said enough for "it does not match" to mean "it is a different paper".
LEGIBLE_FRONT_CHARS = 400
#: A front-matter read is short by nature; below this it counts as no text at all.
MIN_FRONT_CHARS = 200

#: A PDF with fewer than this share of the pages the record spans is a preview or a
#: supplement, not the article. Only a shortfall counts: an accepted manuscript is routinely
#: longer than its typeset page range.
TRUNCATION_RATIO = 0.5
#: Below this many expected pages the ratio is too noisy to act on.
TRUNCATION_MIN_PAGES = 4
#: A cover sheet or a trailing blank can pad a scan beyond its printed range.
PAGE_COUNT_SLACK = 2
PAGE_COUNT_MIN_SPAN = 2

_PAGE_RANGE_RE = re.compile(r"^\s*([A-Za-z]*)(\d+)\s*(?:[-–—]\s*(?:[A-Za-z]*)(\d+))?\s*$")

#: Embedded titles that carry no identity: converter leftovers, filenames, template defaults.
_GENERIC_TITLE_RE = re.compile(
    r"^(untitled|microsoft word|microsoft powerpoint|document|manuscript"
    r"|paper|article|print|pdf|slide|draft|final|revised|proof|template"
    r"|no title|title)?[\s\-_.]*(document|file|manuscript|copy|version|\d+)?$",
    re.IGNORECASE,
)
_FILENAME_TITLE_RE = re.compile(
    r"^[\w \-.]{1,60}\.(tif|tiff|pdf|doc|docx|rtf|qxd|indd|eps|ps|xps|pmd|cdr|tex|dvi)$",
    re.IGNORECASE,
)


@dataclass
class IdentityVerdict:
    state: str
    reason: str
    signals: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == VERIFIED

    @property
    def positive_rejection(self) -> bool:
        return self.state in POSITIVE_REJECTIONS

    def to_dict(self) -> dict:
        return {"state": self.state, "reason": self.reason, "signals": list(self.signals)}


@dataclass
class ExpectedIdentity:
    """What the caller knows about the paper it asked for. Every field optional."""
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    title: Optional[str] = None
    #: Crossref's printed page range, e.g. "107-122". Usually absent for preprints.
    page_range: Optional[str] = None
    #: Where the bytes actually came from, after redirects. Used for one signal only: a file
    #: arXiv itself served for the requested id is that paper, whatever title it now carries.
    source_url: Optional[str] = None

    def __post_init__(self) -> None:
        # Semantic Scholar records for arXiv papers routinely carry the 10.48550/arXiv.* DOI
        # and no ArXiv id. arXiv prints the id on the PDF and never the DOI, so without this
        # every such paper reached the title check -- and failed it whenever the authors had
        # retitled a later version. Measured: 5 of 500 corpus PDFs, all correct files.
        if not self.arxiv_id and self.doi:
            self.arxiv_id = extract_arxiv_id_from_doi(self.doi)

    def is_empty(self) -> bool:
        return not (self.doi or self.arxiv_id or self.title)


# --- text normalisation -----------------------------------------------------------------

def squash(text: Optional[str]) -> str:
    """Lowercased alphanumerics only.

    Dropping everything else is what lets a title broken across a line, or hyphenated at the
    break, match the title it came from. NFKD decomposition turns the ligature glyphs a PDF
    hands back ("identiﬁcation") into plain letters and strips accents. ``str.isalnum`` rather
    than ``[a-z0-9]`` so a Cyrillic or CJK title does not squash to nothing.
    """
    unescaped = _html.unescape(text or "")
    decomposed = unicodedata.normalize("NFKD", unescaped).lower()
    return "".join(ch for ch in decomposed if ch.isalnum())


def matched_fraction(needle: str, haystack: str, min_block: int = 4) -> float:
    """How much of ``needle`` appears in ``haystack``, summed over all matching runs.

    ``SequenceMatcher.ratio()`` is the wrong question: it is symmetric and length-sensitive,
    so a 90-character title against 4,000 characters of page scores near zero however
    perfectly the title appears. Summing all blocks rather than taking the longest keeps one
    substituted character from halving the score. ``min_block`` keeps it honest: without it a
    page of prose supplies a matching letter for nearly every character of any title.
    """
    import difflib

    if not needle or not haystack:
        return 0.0
    matcher = difflib.SequenceMatcher(None, needle, haystack, autojunk=False)
    total = sum(b.size for b in matcher.get_matching_blocks() if b.size >= min_block)
    return min(1.0, total / float(len(needle)))


def normalise_doi(doi: Optional[str]) -> str:
    doi = (doi or "").strip().lower()
    for prefix in ("doi:", "https://doi.org/", "http://doi.org/", "doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/", "dx.doi.org/"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi


def expected_page_count(page_range: Optional[str]) -> Optional[int]:
    """How many printed pages "107-122" implies; None when it implies nothing.

    Only a single page or a simple range is used. Anything else declines to guess, because a
    wrong expectation here refuses a correct file.
    """
    if not page_range:
        return None
    matched = _PAGE_RANGE_RE.match(str(page_range))
    if not matched:
        return None
    prefix, first, last_text = matched.group(1), int(matched.group(2)), matched.group(3)
    if last_text is None:
        # A bare number is one page. A lettered one ("e12345", "R42") is an article number,
        # which says nothing about length.
        return None if prefix else 1
    last = int(last_text)
    if last < first:
        # "1049-58" is 1049-1058.
        digits = len(str(last))
        if digits < len(str(first)):
            last = int(str(first)[:-digits] + str(last))
    span = last - first + 1
    return span if 1 <= span <= 400 else None


# --- the PDF reader ---------------------------------------------------------------------

def engine_available() -> bool:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


class _Reader:
    """One document, parsed once, read lazily."""

    def __init__(self, source) -> None:
        self._source = source
        self._reader = None
        self._failed = False
        self._text: dict[Optional[int], Optional[str]] = {}
        self._pages: Optional[int] = None
        self._metadata: Optional[dict] = None

    def _open(self):
        if self._reader is not None or self._failed:
            return self._reader
        try:
            from pypdf import PdfReader

            src = io.BytesIO(bytes(self._source)) if isinstance(self._source, (bytes, bytearray)) else str(self._source)
            reader = PdfReader(src)
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    pass
            self._reader = reader
        except Exception:
            self._failed = True
        return self._reader

    def text(self, pages: Optional[int]) -> Optional[str]:
        """Text of the first ``pages`` pages (None = whole document), or None if none."""
        if pages in self._text:
            return self._text[pages]
        reader = self._open()
        result: Optional[str] = None
        if reader is not None:
            chunks: list[str] = []
            try:
                for index, page in enumerate(reader.pages):
                    if pages is not None and index >= pages:
                        break
                    try:
                        chunks.append(page.extract_text() or "")
                    except Exception:
                        chunks.append("")
            except Exception:
                pass
            joined = "\n".join(chunks)
            floor = 1 if pages is None else MIN_FRONT_CHARS
            result = joined if len(joined.strip()) >= floor else None
        self._text[pages] = result
        return result

    def page_count(self) -> Optional[int]:
        if self._pages is None:
            reader = self._open()
            try:
                self._pages = len(reader.pages) if reader is not None else 0
            except Exception:
                self._pages = 0
        return self._pages or None

    def embedded_title(self) -> str:
        if self._metadata is None:
            reader = self._open()
            meta: dict = {}
            try:
                if reader is not None and reader.metadata:
                    meta = {str(k).lstrip("/").lower(): str(v) for k, v in reader.metadata.items()}
            except Exception:
                meta = {}
            self._metadata = meta
        return self._metadata.get("title", "") or ""


# --- the verdict ------------------------------------------------------------------------

def verify_pdf_identity(source, expected: ExpectedIdentity) -> IdentityVerdict:
    """Whether the PDF at ``source`` (a path, or bytes) is the paper ``expected`` describes.

    Signals are checked cheapest-first and the first hit wins.
    """
    if not engine_available():
        return IdentityVerdict(NO_ENGINE, "pypdf is not installed, so the PDF cannot be checked against its record")
    if expected.is_empty():
        return IdentityVerdict(NO_REFERENCE, "no DOI, arXiv id or title known, so there is nothing to check the file against")

    reader = _Reader(source)
    truncated = _truncation_verdict(reader, expected.page_range)

    front = reader.text(DOI_PAGES)
    if not front:
        corroborated = _page_count_corroborates(reader, expected.page_range)
        if corroborated:
            return truncated or IdentityVerdict(VERIFIED, corroborated, ["page-count-corroborated"])
        return IdentityVerdict(UNREADABLE, "PDF has no readable text layer, so it cannot be checked against its record")

    squashed_front = squash(front)

    # S1: the requested DOI, printed on the paper. True of nearly every version of record.
    if expected.doi:
        wanted = squash(normalise_doi(expected.doi))
        if wanted and wanted in squashed_front:
            return truncated or IdentityVerdict(VERIFIED, "requested DOI printed in the PDF", ["doi-in-text"])

    # S1b: the arXiv id, stamped down the margin of every PDF arXiv serves. The "arxiv"
    # prefix is required: squashed, the id alone is nine digits and would match a grant
    # number. Version suffix ignored -- v1 and v3 are the same paper.
    if expected.arxiv_id:
        unversioned = re.sub(r"v\d+$", "", str(expected.arxiv_id).strip())
        stamped = "arxiv" + squash(unversioned)
        if stamped != "arxiv" and stamped in squashed_front:
            return truncated or IdentityVerdict(VERIFIED, f"arXiv id {unversioned} stamped on the PDF", ["arxiv-id-in-text"])

    # S1c: arXiv served this file for the requested id. Some author-uploaded PDFs carry no
    # margin stamp at all, and a retitled later version fails every title check while being
    # exactly the paper asked for. The host is what makes this safe: only arXiv's own
    # id-addressed endpoint counts, never a mirror or a landing page.
    if expected.arxiv_id and _arxiv_served_id(expected.source_url, expected.arxiv_id):
        return truncated or IdentityVerdict(
            VERIFIED, f"arXiv served this file for the requested id {expected.arxiv_id}", ["arxiv-id-in-url"])

    wanted_title = squash(expected.title)

    # S2/S3: the title on the front pages. This carries accepted manuscripts, which print
    # the title and not the publisher's DOI.
    if wanted_title:
        title_text = reader.text(TITLE_PAGES) or front
        squashed_title_pages = squash(title_text)
        if wanted_title in squashed_title_pages:
            return truncated or IdentityVerdict(VERIFIED, "article title found on the front pages", ["title-on-page"])
        fraction = matched_fraction(wanted_title, squashed_title_pages)
        if fraction >= TITLE_BLOCK_MIN:
            return truncated or IdentityVerdict(
                VERIFIED, f"article title matches the front pages ({fraction:.0%} of it)", ["title-near-match"])
        if fraction >= TITLE_BLOCK_PARTIAL and _page_count_corroborates(reader, expected.page_range):
            return truncated or IdentityVerdict(
                VERIFIED,
                f"article title partly legible ({fraction:.0%}) and the PDF is as long as the record ({expected.page_range})",
                ["title-partial-with-page-count"],
            )

    # S4: the title the document claims for itself.
    embedded = reader.embedded_title()
    if wanted_title and embedded and not _is_generic_title(embedded):
        squashed_embedded = squash(embedded)
        embedded_fraction = matched_fraction(wanted_title, squashed_embedded)
        if embedded_fraction >= TITLE_BLOCK_MIN:
            return truncated or IdentityVerdict(VERIFIED, "embedded PDF title matches the article title", ["title-in-metadata"])
        # A short embedded title cannot disagree with a long one, it can only fail to
        # contain it. Only a title long enough to have matched is allowed to contradict.
        could_have_matched = bool(squashed_embedded) and len(squashed_embedded) >= len(wanted_title) * TITLE_BLOCK_MIN
        if could_have_matched and embedded_fraction < TITLE_BLOCK_FOREIGN and len(squashed_front) >= LEGIBLE_FRONT_CHARS:
            return IdentityVerdict(
                WRONG, f'PDF is a different document: it calls itself "{_clip(embedded)}", not "{_clip(expected.title)}"')

    # Nothing matched.
    if wanted_title:
        if len(squashed_front) < LEGIBLE_FRONT_CHARS:
            # A page carrying almost no machine-readable text has not said anything and
            # cannot be convicted of being a different article.
            corroborated = _page_count_corroborates(reader, expected.page_range)
            if corroborated:
                return truncated or IdentityVerdict(VERIFIED, corroborated, ["page-count-corroborated"])
            return IdentityVerdict(
                UNREADABLE,
                f"PDF has almost no readable text ({len(squashed_front)} characters over {DOI_PAGES} pages), so it cannot be checked")
        return IdentityVerdict(WRONG, f'PDF does not contain the requested DOI or the title "{_clip(expected.title)}"')
    return IdentityVerdict(NO_REFERENCE, "no title known and the DOI is not printed in the PDF, so there is nothing to check against")


def _truncation_verdict(reader: _Reader, page_range: Optional[str]) -> Optional[IdentityVerdict]:
    """A verdict when the file is only a fragment of the record, else None.

    Invisible to every identity signal: a publisher's first-page preview and an article's own
    supplementary figures both carry the real DOI and title. What gives them away is length.
    """
    expected = expected_page_count(page_range)
    if not expected or expected < TRUNCATION_MIN_PAGES:
        return None
    actual = reader.page_count()
    if not actual or actual >= expected * TRUNCATION_RATIO:
        return None
    return IdentityVerdict(
        TRUNCATED, f"PDF is only {actual} of the {expected} pages this record spans ({page_range}) -- a preview or a supplement")


def _page_count_corroborates(reader: _Reader, page_range: Optional[str]) -> Optional[str]:
    """A reason when the PDF's page count matches the record's printed range, else None.

    The only identity evidence an image-only scan can offer, and it is a property of the
    file, not of our metadata.
    """
    expected = expected_page_count(page_range)
    if not expected or expected < PAGE_COUNT_MIN_SPAN:
        return None
    actual = reader.page_count()
    if not actual:
        return None
    if expected <= actual <= expected + PAGE_COUNT_SLACK:
        return f"PDF has no readable text, but its {actual} pages match the {expected} pages this record spans ({page_range})"
    return None


def _arxiv_served_id(url: Optional[str], arxiv_id: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host not in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"):
        return False
    unversioned = re.sub(r"v\d+$", "", str(arxiv_id).strip()).lower()
    path = parsed.path.lower()
    return bool(unversioned) and (
        path.startswith(f"/pdf/{unversioned}") or path.startswith(f"/abs/{unversioned}"))


def _is_generic_title(embedded: str) -> bool:
    stripped = (embedded or "").strip()
    return bool(_GENERIC_TITLE_RE.match(stripped) or _FILENAME_TITLE_RE.match(stripped))


def _clip(text: Optional[str], limit: int = 70) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
