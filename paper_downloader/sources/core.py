from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


import requests

from paper_downloader.core import host_gate

from paper_downloader.config.models import ApiConfig, DownloadConfig, ResolutionConfig
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import SourceCandidate, validate_title_match


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        domain = (urlparse(url).netloc or "").lower().strip()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain or None
    except Exception:
        return None


def _title_query(title: str) -> str:
    """CORE's phrase search for a title.

    The parentheses are load-bearing and easy to lose. `title:"Attention Is All You Need"`
    returns 3.1 million hits -- the quotes are not honoured, so every common word matches
    separately and the top five results are effectively random. `title:("Attention Is All
    You Need")` returns 56. For a distinctive title both forms return the same one hit, so
    the parenthesised form is never worse and is dramatically better for any title made of
    ordinary words.
    """
    cleaned = title.strip().replace('"', " ")
    return f'title:("{cleaned}")'


@dataclass(slots=True)
class CORESourceProvider:
    

    api_config: ApiConfig
    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    base_url: str = "https://api.core.ac.uk/v3"
    name: str = "core"
    _session: requests.Session = field(default=None, init=False, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._session = host_gate.GatedSession()
        self._session.headers.update(self._headers())

    def __del__(self) -> None:
        if self._session is not None:
            self._session.close()

    def resolve(self, paper: PaperRecord) -> list[SourceCandidate]:
        candidates: list[SourceCandidate] = []

        if paper.doi:
            works = self._search(f'doi:"{paper.doi.strip()}"', limit=3)
            for work in works:
                candidates.extend(
                    self._candidates_from_work(
                        work=work,
                        exact_lookup=True,
                        title_match_score=1.0 if paper.title else None,
                    )
                )

        # There used to be an arXiv-id branch here. CORE has no queryable arXiv field --
        # `arxivId:`, `arxiv:` and `identifiers.arxivId:` all answer HTTP 500 -- so it was a
        # request that could only ever fail, and it made the provider look broken in the run
        # log. Nothing is lost: the `arxiv` provider handles arXiv ids directly, and CORE's
        # value here is repository copies of papers that are not on arXiv at all.

        if not candidates and self.resolution_config.allow_title_fallback and paper.title:
            works = self._search(_title_query(paper.title), limit=5)
            for work in works:
                work_title = work.get("title") or work.get("displayTitle")
                score = validate_title_match(paper.title, work_title)
                if score < self.resolution_config.title_similarity_threshold:
                    continue
                if paper.year is not None:
                    work_year = work.get("yearPublished")
                    if isinstance(work_year, int) and abs(work_year - paper.year) > 1:
                        continue
                candidates.extend(
                    self._candidates_from_work(
                        work=work,
                        exact_lookup=False,
                        title_match_score=score,
                    )
                )

        return self._deduplicate(candidates)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.download_config.user_agent,
        }
        if self.api_config.core_api_key:
            headers["Authorization"] = f"Bearer {self.api_config.core_api_key}"
        return headers

    def _timeout(self) -> tuple[int, int]:
        return (
            self.download_config.connect_timeout_seconds,
            self.download_config.read_timeout_seconds,
        )

    def _search(self, q: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """
        GET /v3/search/works?q=<query>&limit=<n>
        q supports Elasticsearch field syntax: doi:"...", arxivId:"...", title:"..."
        """
        try:
            response = self._session.get(
                f"{self.base_url}/search/works",
                params={"q": q, "limit": limit},
                timeout=self._timeout(),
                allow_redirects=True,
                verify=self.download_config.verify_ssl,
            )
        except requests.RequestException:
            return []

        if response.status_code == 429:
            return []
        if response.status_code >= 400:
            return []

        try:
            payload = response.json()
        except ValueError:
            return []

        if not isinstance(payload, dict):
            return []

        results = payload.get("results") or []
        return [r for r in results if isinstance(r, dict)]

    def _candidates_from_work(
        self,
        *,
        work: dict[str, Any],
        exact_lookup: bool,
        title_match_score: float | None,
    ) -> list[SourceCandidate]:
        base_confidence = 0.80 if exact_lookup else 0.65

        pdf_urls: list[str] = []

        download_url = work.get("downloadUrl")
        if isinstance(download_url, str) and download_url.strip():
            pdf_urls.append(download_url.strip())

        full_text_id = work.get("fullTextIdentifier")
        if isinstance(full_text_id, str) and full_text_id.strip():
            pdf_urls.append(full_text_id.strip())

        for url in (work.get("sourceFulltextUrls") or []):
            if isinstance(url, str) and url.strip():
                pdf_urls.append(url.strip())

        for link in (work.get("links") or []):
            if not isinstance(link, dict):
                continue
            link_url = link.get("url")
            link_type = (link.get("type") or "").lower()
            if isinstance(link_url, str) and link_url.strip():
                if link_type in {"download", "pdf", "fulltext"} or link_url.lower().endswith(".pdf"):
                    pdf_urls.append(link_url.strip())

        seen_urls: set[str] = set()
        candidates: list[SourceCandidate] = []

        work_title = work.get("title") or work.get("displayTitle")
        work_doi = work.get("doi")
        core_id = work.get("id")
        year = work.get("yearPublished")
        publisher = work.get("publisher")
        journals = work.get("journals") or []
        journal_name = journals[0].get("title") if journals and isinstance(journals[0], dict) else None

        for pdf_url in pdf_urls:
            if pdf_url in seen_urls:
                continue
            seen_urls.add(pdf_url)

            domain = _domain_from_url(pdf_url)
            if domain and "core.ac.uk" in domain:
                host_type = "repository"
                version_type = "accepted"
            else:
                host_type = "unknown"
                version_type = "unknown"

            candidates.append(
                SourceCandidate(
                    source_name=self.name,
                    pdf_url=pdf_url,
                    landing_page_url=f"https://core.ac.uk/works/{core_id}" if core_id else None,
                    version_type=version_type,
                    host_type=host_type,
                    license=None,
                    domain=domain,
                    confidence=base_confidence,
                    is_direct_pdf=pdf_url.lower().endswith(".pdf") or "core.ac.uk" in (domain or ""),
                    title_match_score=title_match_score,
                    reason="core doi lookup" if exact_lookup else "core title search",
                    metadata={
                        "core_id": core_id,
                        "work_title": work_title,
                        "work_doi": work_doi,
                        "year": year,
                        "publisher": publisher,
                        "journal": journal_name,
                    },
                )
            )

        return candidates

    def _deduplicate(self, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
        deduped: dict[str, SourceCandidate] = {}
        for candidate in candidates:
            key = candidate.pdf_url
            existing = deduped.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                deduped[key] = candidate
        return list(deduped.values())