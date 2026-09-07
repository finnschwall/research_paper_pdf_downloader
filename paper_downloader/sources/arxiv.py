"""arXiv: by id when we have one, by title search when we do not.

Most computer-science papers behind a publisher wall have a preprint on arXiv, but the
id only reaches this library when Semantic Scholar recorded it. For an IEEE or ACM paper
whose record lacks it, the provider used to do nothing at all. The title search asks the
arXiv API (``export.arxiv.org``, one request per three seconds -- see ``host_gate``) and
accepts a hit only when the title matches *and* the authors overlap, because a title alone
is how a systematic review ends up citing the wrong paper.

Withdrawn preprints are recognised (``<arxiv:comment>``) and never offered: a review should
not silently include a paper its authors pulled. The reason is reported so the caller can
record ``withdrawn`` rather than "no copy found".

The exact-id path is the exception, and deliberately so. Given an arXiv id this provider
builds the PDF URL without asking the API at all, which is what makes it the fastest route in
the library -- but it therefore never sees the withdrawal note. arXiv answers a withdrawn
paper's PDF URL with a 404, and a bare 404 reads as a broken link. So the check is deferred
rather than dropped: ``withdrawal_note`` asks the API for exactly one id, and the download
stage calls it only after an arXiv candidate has actually 404'd. One extra request for the
rare withdrawn paper, none for the other thousands.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import requests

from paper_downloader.config.models import DownloadConfig, ResolutionConfig
from paper_downloader.core import host_gate
from paper_downloader.metadata.id_recovery import normalize_arxiv_id
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import SourceCandidate, validate_title_match

_API_URL = "https://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"

#: Only letters, digits and spaces survive into the query. The API's own parser trips on
#: colons, quotes and parentheses inside a quoted phrase.
_QUERY_CLEAN_RE = re.compile(r"[^A-Za-z0-9 ]+")
_SPACE_RE = re.compile(r"\s+")

#: How many surnames must match. One when the paper has a single author.
_MIN_AUTHOR_OVERLAP = 2

#: A preprint may precede the published version by this much, or follow it by one year.
_YEARS_BEFORE = 2
_YEARS_AFTER = 1

_TITLE_SEARCH_CONFIDENCE = 0.80


@dataclass(slots=True)
class ArxivHit:
    arxiv_id: str
    title: str
    authors: list[str]
    year: int | None
    comment: str

    @property
    def withdrawn(self) -> bool:
        return "withdrawn" in self.comment.lower()


def parse_atom_feed(text: str) -> list[ArxivHit]:
    """The entries of an arXiv API Atom feed. Malformed XML yields no hits, not an error."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    hits: list[ArxivHit] = []
    for entry in root.findall(f"{_ATOM}entry"):
        raw_id = (entry.findtext(f"{_ATOM}id") or "").strip()
        arxiv_id = normalize_arxiv_id(raw_id)
        if not arxiv_id:
            continue
        title = _SPACE_RE.sub(" ", entry.findtext(f"{_ATOM}title") or "").strip()
        authors = [
            _SPACE_RE.sub(" ", name).strip()
            for name in (
                author.findtext(f"{_ATOM}name") or "" for author in entry.findall(f"{_ATOM}author")
            )
            if name.strip()
        ]
        published = (entry.findtext(f"{_ATOM}published") or "").strip()
        year = int(published[:4]) if published[:4].isdigit() else None
        comment = (entry.findtext(f"{_ARXIV}comment") or "").strip()
        hits.append(ArxivHit(arxiv_id=arxiv_id, title=title, authors=authors, year=year, comment=comment))
    return hits


def surname(name: str) -> str:
    """Last token of a name, lowercased and stripped of punctuation. 'Anke Tang' -> 'tang'."""
    cleaned = _QUERY_CLEAN_RE.sub(" ", name).strip().lower()
    if "," in name:  # "Tang, Anke"
        cleaned = _QUERY_CLEAN_RE.sub(" ", name.split(",", 1)[0]).strip().lower()
    parts = cleaned.split()
    return parts[-1] if parts else ""


def authors_overlap(paper_authors: list[str], hit_authors: list[str]) -> int:
    ours = {surname(a) for a in paper_authors} - {""}
    theirs = {surname(a) for a in hit_authors} - {""}
    return len(ours & theirs)


def clean_title_for_query(title: str) -> str:
    return _SPACE_RE.sub(" ", _QUERY_CLEAN_RE.sub(" ", title)).strip()


@dataclass(slots=True)
class ArxivSourceProvider:
    download_config: DownloadConfig | None = None
    resolution_config: ResolutionConfig | None = None
    name: str = "arxiv"
    _session: requests.Session | None = field(default=None, init=False, repr=False)
    last_reason: str | None = field(default=None, init=False, repr=False)
    last_failed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.download_config is None:
            self.download_config = DownloadConfig()
        if self.resolution_config is None:
            self.resolution_config = ResolutionConfig()

    def __del__(self) -> None:
        if self._session is not None:
            self._session.close()

    def resolve(self, paper: PaperRecord) -> list[SourceCandidate]:
        self.last_reason = None
        self.last_failed = False

        if paper.arxiv_id:
            return [self._candidate(paper.arxiv_id, confidence=0.92, reason="exact arxiv id")]

        if not self.resolution_config.allow_title_fallback or not paper.title:
            self.last_reason = "no arxiv id" + ("" if paper.title else ", no title to search")
            return []

        hits = self._search_title(paper.title)
        if hits is None:
            return []
        if not hits:
            self.last_reason = "no arxiv id, title search 0 hits"
            return []

        rejected: list[str] = []
        for hit in hits:
            verdict = self._judge(paper, hit)
            if verdict is None:
                if hit.withdrawn:
                    # Matched, but pulled by its authors or by arXiv. Not a copy to offer.
                    self.last_reason = f"arxiv {hit.arxiv_id} matches but is withdrawn: {hit.comment[:120]}"
                    return []
                return [
                    self._candidate(
                        hit.arxiv_id,
                        confidence=_TITLE_SEARCH_CONFIDENCE,
                        reason="arxiv title search",
                        title_match_score=validate_title_match(paper.title, hit.title),
                        extra={"matched_title": hit.title, "matched_authors": hit.authors},
                    )
                ]
            rejected.append(f"{hit.arxiv_id}: {verdict}")

        self.last_reason = f"no arxiv id, {len(hits)} title hit(s) rejected ({'; '.join(rejected)[:300]})"
        return []

    def _judge(self, paper: PaperRecord, hit: ArxivHit) -> str | None:
        """None when the hit is this paper; otherwise why it is not."""
        threshold = self.resolution_config.title_similarity_threshold
        score = validate_title_match(paper.title, hit.title)
        if score < threshold:
            return f"title similarity {score:.2f} < {threshold:.2f}"

        if paper.authors:
            need = 1 if len(paper.authors) == 1 else _MIN_AUTHOR_OVERLAP
            overlap = authors_overlap(paper.authors, hit.authors)
            if overlap < need:
                return f"{overlap} author surname(s) in common, need {need}"

        if paper.year is not None and hit.year is not None:
            if hit.year < paper.year - _YEARS_BEFORE or hit.year > paper.year + _YEARS_AFTER:
                return f"published {hit.year}, paper is {paper.year}"

        return None

    def _search_title(self, title: str) -> list[ArxivHit] | None:
        """Hits for the title, [] for none, None when the request itself failed."""
        query = clean_title_for_query(title)
        if not query:
            self.last_reason = "title has no searchable characters"
            return []
        try:
            response = self._get_session().get(
                _API_URL,
                params={"search_query": f'ti:"{query}"', "max_results": "5"},
                timeout=(
                    self.download_config.connect_timeout_seconds,
                    self.download_config.read_timeout_seconds,
                ),
                verify=self.download_config.verify_ssl,
            )
        except requests.RequestException as exc:
            self.last_failed = True
            self.last_reason = f"arxiv api request failed: {exc.__class__.__name__}"
            return None
        if response.status_code >= 400:
            self.last_failed = True
            self.last_reason = f"arxiv api http {response.status_code}"
            return None
        return parse_atom_feed(response.text)

    def withdrawal_note(self, arxiv_id: str) -> str | None:
        """arXiv's own note on why this id was withdrawn, or None if it was not.

        One API call, made only when the PDF URL for a known id has already answered 404 --
        which for arXiv means the paper was pulled, since the id itself was valid enough to
        reach the endpoint. Returns None on any doubt, including a failed request: reporting
        a paper as withdrawn is a decision a review acts on, so it needs the note itself.
        """
        cleaned = normalize_arxiv_id(arxiv_id) or (arxiv_id or "").strip()
        if not cleaned:
            return None
        try:
            response = self._get_session().get(
                _API_URL,
                params={"id_list": cleaned, "max_results": "1"},
                timeout=(
                    self.download_config.connect_timeout_seconds,
                    self.download_config.read_timeout_seconds,
                ),
                verify=self.download_config.verify_ssl,
            )
        except requests.RequestException:
            return None
        if response.status_code >= 400:
            return None
        for hit in parse_atom_feed(response.text):
            if hit.withdrawn:
                return hit.comment or "withdrawn"
        return None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = host_gate.GatedSession()
            self._session.headers.update({
                "Accept": "application/atom+xml",
                "User-Agent": self.download_config.user_agent,
            })
        return self._session

    def _candidate(
        self,
        arxiv_id: str,
        *,
        confidence: float,
        reason: str,
        title_match_score: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> SourceCandidate:
        return SourceCandidate(
            source_name=self.name,
            pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
            landing_page_url=f"https://arxiv.org/abs/{arxiv_id}",
            version_type="preprint",
            host_type="preprint",
            license=None,
            domain="arxiv.org",
            confidence=confidence,
            is_direct_pdf=True,
            title_match_score=title_match_score,
            reason=reason,
            metadata={"arxiv_id": arxiv_id, **(extra or {})},
        )
