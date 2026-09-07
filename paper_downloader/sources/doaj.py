from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests

from paper_downloader.core import host_gate

from paper_downloader.config.models import DownloadConfig, ResolutionConfig
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


@dataclass(slots=True)
class DOAJSourceProvider:
    

    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    base_url: str = "https://doaj.org/api"
    name: str = "doaj"
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
            articles = self._search_by_doi(paper.doi)
            for article in articles:
                candidates.extend(
                    self._candidates_from_article(
                        article,
                        exact_lookup=True,
                        title_match_score=1.0 if paper.title else None,
                    )
                )

        if not candidates and self.resolution_config.allow_title_fallback and paper.title:
            articles = self._search_by_title(paper.title)
            for article in articles:
                bibjson = article.get("bibjson") or {}
                article_title = bibjson.get("title")
                score = validate_title_match(paper.title, article_title)
                if score < self.resolution_config.title_similarity_threshold:
                    continue

                if paper.year is not None:
                    year_raw = bibjson.get("year")
                    if isinstance(year_raw, str) and year_raw.isdigit():
                        if abs(int(year_raw) - paper.year) > 1:
                            continue

                candidates.extend(
                    self._candidates_from_article(
                        article,
                        exact_lookup=False,
                        title_match_score=score,
                    )
                )

        return self._deduplicate(candidates)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": self.download_config.user_agent,
        }

    def _timeout(self) -> tuple[int, int]:
        return (
            self.download_config.connect_timeout_seconds,
            self.download_config.read_timeout_seconds,
        )

    def _search(self, query: str, *, page_size: int = 5) -> list[dict[str, Any]]:
       
       
        escaped_query = query.replace("/", "\\/")
        encoded_query = quote(escaped_query, safe="")

        url = f"{self.base_url}/search/articles/{encoded_query}"

        try:
            response = self._session.get(
                url,
                params={"pageSize": str(page_size)},
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

    def _search_by_doi(self, doi: str) -> list[dict[str, Any]]:
       
        return self._search(f"doi:{doi.strip()}", page_size=3)

    def _search_by_title(self, title: str) -> list[dict[str, Any]]:
        
        return self._search(f'title:"{title}"', page_size=5)

    def _candidates_from_article(
        self,
        article: dict[str, Any],
        *,
        exact_lookup: bool,
        title_match_score: float | None,
    ) -> list[SourceCandidate]:
        bibjson = article.get("bibjson") or {}

        article_title = bibjson.get("title")
        article_doi = self._extract_doi(bibjson)
        journal_info = bibjson.get("journal") or {}
        journal_title = journal_info.get("title")
        publisher = journal_info.get("publisher")
        year = bibjson.get("year")

        pdf_url, landing_url = self._extract_urls(bibjson)
        if not pdf_url and not landing_url:
            return []

        effective_pdf_url = pdf_url or landing_url
        if not effective_pdf_url:
            return []

        base_confidence = 0.82 if exact_lookup else 0.62

        return [
            SourceCandidate(
                source_name=self.name,
                pdf_url=effective_pdf_url,
                landing_page_url=landing_url,
                version_type="publisher",
                host_type="publisher",
                license=bibjson.get("license", [{}])[0].get("type") if bibjson.get("license") else None,
                domain=_domain_from_url(effective_pdf_url),
                confidence=base_confidence,
                is_direct_pdf=(
                    effective_pdf_url.lower().endswith(".pdf")
                    or "/pdf" in effective_pdf_url.lower()
                ),
                title_match_score=title_match_score,
                # Listed in DOAJ means the journal is open access: a free copy exists.
                asserts_open_access=True,
                reason="doaj doi lookup" if exact_lookup else "doaj title search",
                metadata={
                    "work_title": article_title,
                    "work_doi": article_doi,
                    "journal": journal_title,
                    "publisher": publisher,
                    "year": year,
                },
            )
        ]

    def _extract_doi(self, bibjson: dict[str, Any]) -> str | None:
        identifiers = bibjson.get("identifier") or []
        for entry in identifiers:
            if not isinstance(entry, dict):
                continue
            if (entry.get("type") or "").lower() == "doi":
                return entry.get("id")
        return None

    def _extract_urls(
        self, bibjson: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        
        links = bibjson.get("link") or []
        pdf_url: str | None = None
        landing_url: str | None = None

        for link in links:
            if not isinstance(link, dict):
                continue
            link_type = (link.get("type") or "").lower()
            url = link.get("url") or ""
            content_type = (link.get("content_type") or "").lower()

            if not url:
                continue

            if link_type == "fulltext":
                if content_type == "application/pdf" or url.lower().endswith(".pdf"):
                    pdf_url = url
                else:
                    landing_url = url

        return pdf_url, landing_url

    def _deduplicate(self, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
        deduped: dict[str, SourceCandidate] = {}
        for candidate in candidates:
            key = candidate.pdf_url
            existing = deduped.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                deduped[key] = candidate
        return list(deduped.values())