"""What kind of record this DOI is, decided before anything is fetched.

A download failure says what happened when we tried. It cannot say that there was never
anything to try for. A conference abstract, a withdrawn preprint and a paywalled article all
end up as "no PDF", so a review's accounting cannot tell "this paper has no full text
anywhere" from "we could not get at it", and a "retry the failures" button re-sends things
that can never succeed.

The record class is the other half: a property of the *record*, read off metadata Crossref
and OpenAlex already hold. For the terminal classes the pipeline stops before resolution --
no provider calls, no publisher requests, and a status a caller can file under "nothing to
fetch" rather than under "could not retrieve".

``retracted`` is deliberately not terminal. A retracted article usually still has a PDF, and
a screener needs to see it in order to exclude the paper for the right reason.

Signals that were measured and rejected, so nobody adds them back: Semantic Scholar's
``publicationTypes`` (it says ``JournalArticle`` for meeting abstracts, posters and
withdrawn preprints alike); a single-page page range (MDPI and IOP article numbers look
exactly like abstract numbers); and a bare ``^Abstract\\b`` title match (it hits real papers
with "Abstract" in the title).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: A record with nothing to fetch, or that is not the paper the review asked for. The
#: pipeline skips resolution and download for these and reports ``skipped_<class>``.
CONFERENCE_ABSTRACT = "conference_abstract"
POSTER = "poster"
WITHDRAWN = "withdrawn"
PARATEXT = "paratext"
NOT_AN_ARTICLE = "not_an_article"
CORRECTION = "correction"

#: A real paper with a real PDF that a screener must be told about. Never terminal.
RETRACTED = "retracted"

TERMINAL_CLASSES: frozenset[str] = frozenset({
    CONFERENCE_ABSTRACT, POSTER, WITHDRAWN, PARATEXT, NOT_AN_ARTICLE, CORRECTION,
})

RECORD_CLASSES: tuple[str, ...] = (
    CONFERENCE_ABSTRACT, POSTER, WITHDRAWN, RETRACTED, PARATEXT, NOT_AN_ARTICLE, CORRECTION,
)

#: Title shapes that only meeting abstracts have. Each is a conference's own numbering
#: scheme, not a guess: AACR prints "Abstract 4137:", ESMO "316P", the ATS "A1234".
#: On their own these are a weak signal -- they must be corroborated (see `classify`).
_ABSTRACT_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^abstract\s+[a-z]{0,3}\d+\s*[:.]", re.IGNORECASE),   # AACR "Abstract 4137:"
    re.compile(r"^\d{1,4}[a-z]{1,3}\s+\S", re.IGNORECASE),            # ESMO "316P ", "1234MO "
    re.compile(r"^[a-d]\d{1,4}-\d{1,4}\s+\S", re.IGNORECASE),         # ATS "A1234-5678 "
    re.compile(r"^lba\d+", re.IGNORECASE),                            # "LBA12" late-breaker
)

_CORRECTION_TITLE_RE = re.compile(
    r"^\s*(erratum|corrigendum|correction to|publisher correction|author correction)\b",
    re.IGNORECASE,
)

#: Crossref ``type`` values that are documents but not the article a review wants.
_NOT_AN_ARTICLE_TYPES: frozenset[str] = frozenset({
    "dataset", "peer-review", "book", "standard", "report-component", "component",
    "grant", "database",
})

_PARATEXT_TYPES: frozenset[str] = frozenset({"journal-issue", "journal-volume", "book-series"})

#: Container names that mean "this issue is a book of abstracts". Corroborates a title
#: pattern; on its own it is not enough, because supplements also carry real papers.
_ABSTRACT_CONTAINER_RE = re.compile(
    r"(abstract|supplement|proceedings of the .*(meeting|congress|symposium))", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class RecordVerdict:
    """The class, why it was decided, and the two flags a caller stores alongside it."""
    record_class: str | None = None
    reason: str = ""
    retracted: bool = False
    #: Crossref's ``published-online`` date as ``YYYY-MM-DD``, when it has one. Used to tell
    #: "the publisher has not put the PDF up yet" from "the publisher will not give it to us".
    published_online: str | None = None

    @property
    def terminal(self) -> bool:
        return self.record_class in TERMINAL_CLASSES


def _text(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value).strip() if value else ""


def _crossref_type(work: dict[str, Any] | None) -> str:
    return _text((work or {}).get("type")).lower()


def _crossref_container(work: dict[str, Any] | None) -> str:
    return _text((work or {}).get("container-title"))


def _date_parts_to_iso(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    parts = node.get("date-parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
        return None
    numbers = [int(p) for p in parts[0][:3] if isinstance(p, int)]
    if not numbers:
        return None
    numbers += [1] * (3 - len(numbers))
    return "%04d-%02d-%02d" % tuple(numbers[:3])


def published_online(work: dict[str, Any] | None) -> str | None:
    """The date the publisher put this article online, as ``YYYY-MM-DD``, or None."""
    if not isinstance(work, dict):
        return None
    for field in ("published-online", "published", "issued"):
        found = _date_parts_to_iso(work.get(field))
        if found:
            return found
    return None


def is_retracted(*, crossref: dict[str, Any] | None, openalex: dict[str, Any] | None) -> bool:
    """Has this article been retracted?

    OpenAlex carries a flag; Crossref has held Retraction Watch's data since January 2025 and
    reports it as an ``update-to``/``updated-by`` entry.
    """
    if isinstance(openalex, dict) and openalex.get("is_retracted"):
        return True
    if not isinstance(crossref, dict):
        return False
    for key in ("update-to", "updated-by"):
        for entry in crossref.get(key) or []:
            if isinstance(entry, dict) and "retraction" in _text(entry.get("type")).lower():
                return True
    return False


def _looks_like_abstract_title(title: str) -> bool:
    return any(pattern.match(title) for pattern in _ABSTRACT_TITLE_PATTERNS)


def _conference_abstract(
    *, crossref: dict[str, Any] | None, openalex: dict[str, Any] | None, title: str,
) -> str | None:
    """Reason string when this record is a meeting abstract, else None.

    Two signals are trusted alone because they are the publisher's own statement:
    OpenAlex's ``conference-abstract`` type, and a Crossref issue labelled a supplement of an
    abstract book. A title pattern is trusted only with a matching container, because a
    numbering scheme is a coincidence away from a real paper's title.
    """
    if _text((openalex or {}).get("type")).lower() == "conference-abstract":
        return "openalex type is conference-abstract"

    issue = _text((crossref or {}).get("issue"))
    container = _crossref_container(crossref)
    supplement = bool(re.search(r"suppl", issue, re.IGNORECASE))
    abstract_book = bool(_ABSTRACT_CONTAINER_RE.search(container)) if container else False

    if supplement and abstract_book:
        return f"crossref issue {issue!r} of {container!r}"
    if title and _looks_like_abstract_title(title) and (supplement or abstract_book):
        return f"abstract-numbering title in {container or 'a supplement'!r}"
    return None


def classify(
    *,
    crossref: dict[str, Any] | None = None,
    openalex: dict[str, Any] | None = None,
    title: str | None = None,
) -> RecordVerdict:
    """Decide what kind of record this is from metadata alone. Never makes a request.

    Order matters only in that the first match wins, and the classes are close to disjoint:
    a correction is not an abstract, a poster is not a journal issue. When nothing matches,
    the record is an ordinary paper and ``record_class`` is None -- which is the answer for
    the overwhelming majority.
    """
    title = (title or _text((crossref or {}).get("title"))
             or _text((openalex or {}).get("display_name"))).strip()
    retracted = is_retracted(crossref=crossref, openalex=openalex)
    online = published_online(crossref)

    def verdict(record_class: str | None, reason: str = "") -> RecordVerdict:
        return RecordVerdict(
            record_class=record_class, reason=reason,
            retracted=retracted, published_online=online,
        )

    crossref_type = _crossref_type(crossref)
    openalex_type = _text((openalex or {}).get("type")).lower()

    reason = _conference_abstract(crossref=crossref, openalex=openalex, title=title)
    if reason:
        return verdict(CONFERENCE_ABSTRACT, reason)

    # Morressier and the other poster hosts deposit under a handful of prefixes and register
    # as "posted content of no particular kind". A file may exist; it is a poster, not a paper.
    doi = _text((crossref or {}).get("DOI")).lower()
    subtype = _text((crossref or {}).get("subtype")).lower()
    if doi.startswith("10.26226/"):
        return verdict(POSTER, "doi prefix 10.26226 (morressier poster deposit)")
    if crossref_type == "posted-content" and subtype in {"other", "poster"}:
        return verdict(POSTER, f"crossref posted-content, subtype {subtype!r}")

    if _CORRECTION_TITLE_RE.match(title):
        return verdict(CORRECTION, f"title begins {title[:40]!r}")
    for entry in (crossref or {}).get("update-to") or []:
        if isinstance(entry, dict) and _text(entry.get("type")).lower() in {"correction", "erratum"}:
            return verdict(CORRECTION, "crossref update-to says correction")

    if (openalex or {}).get("is_paratext") or crossref_type in _PARATEXT_TYPES:
        return verdict(PARATEXT, f"paratext (crossref type {crossref_type!r})")

    if crossref_type in _NOT_AN_ARTICLE_TYPES:
        return verdict(NOT_AN_ARTICLE, f"crossref type {crossref_type!r}")

    if openalex_type in {"dataset", "peer-review", "grant", "libguides"}:
        return verdict(NOT_AN_ARTICLE, f"openalex type {openalex_type!r}")

    return verdict(None, "")
