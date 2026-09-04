from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests

from paper_downloader.core import host_gate

from paper_downloader.config.models import ApiConfig, DownloadConfig, ResolutionConfig
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import SourceCandidate, validate_title_match

_BLOCKED_URL_PATTERNS: tuple[str, ...] = (
        "staging.",
        "xplorestaging.",
        "test.",
        "sandbox.",
        "dev.",
        "qa.",
    )

def is_staging_url(url: str) -> bool:
    lowered = url.lower()
    return any(pattern in lowered for pattern in _BLOCKED_URL_PATTERNS)


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


def _map_content_version(raw: str | None) -> str:
    """
    Crossref uses: vor (version of record), am (accepted manuscript),
    tdm (text/data mining), unspecified.
    """
    if not raw:
        return "unknown"
    lowered = raw.lower()
    if lowered == "vor":
        return "publisher"     
    if lowered == "tdm":
        return "publisher" 
    if lowered == "am":
        return "accepted"      
    if lowered in {"preprint", "submitted"}:
        return "preprint"
    return "unknown"


@dataclass(slots=True)
class CrossrefSourceProvider:
    api_config: ApiConfig
    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    base_url: str = "https://api.crossref.org"
    name: str = "crossref"
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
            work = self._fetch_by_doi(paper.doi)
            if work:
                candidates.extend(
                    self._candidates_from_work(
                        work=work,
                        exact_lookup=True,
                        title_match_score=1.0 if paper.title else None,
                    )
                )

        
        if not candidates and self.resolution_config.allow_title_fallback and paper.title:
            for work in self._search_by_title(paper.title):
                work_title = work.get("title", [])
                if isinstance(work_title, list):
                    work_title = work_title[0] if work_title else None

                score = validate_title_match(paper.title, work_title)
                if score < self.resolution_config.title_similarity_threshold:
                    continue

                if paper.year is not None:
                    work_year = (work.get("published") or {}).get("date-parts", [[None]])[0][0]
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

    def _request_json(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any] | None:
        try:
            response = self._session.get(
                url,
                params=params or {},
                timeout=self._timeout(),
                allow_redirects=True,
                verify=self.download_config.verify_ssl,
            )
        except requests.RequestException:
            return None

        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            return None

        try:
            payload = response.json()
        except ValueError:
            return None

        return payload if isinstance(payload, dict) else None

    def _fetch_by_doi(self, doi: str) -> dict[str, Any] | None:
        encoded = quote(doi.strip(), safe="")
        payload = self._request_json(f"{self.base_url}/works/{encoded}")
        if not payload:
            return None
        # Crossref wraps the work in {"status": "ok", "message": {...}}
        message = payload.get("message")
        return message if isinstance(message, dict) else None
    
    

    def _search_by_title(self, title: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            f"{self.base_url}/works",
            params={
                "query.title": title,
                "rows": "5",
                "select": "DOI,title,published,link,author,type",
            },
        )
        if not payload:
            return []
        message = payload.get("message") or {}
        items = message.get("items") or []
        return [item for item in items if isinstance(item, dict)]

    def _candidates_from_work(
        self,
        *,
        work: dict[str, Any],
        exact_lookup: bool,
        title_match_score: float | None,
    ) -> list[SourceCandidate]:
        
        base_confidence = 0.82 if exact_lookup else 0.60
        links: list[dict[str, Any]] = work.get("link") or []
        doi = work.get("DOI")
        work_type = work.get("type", "")

        seen_urls: set[str] = set()
        candidates: list[SourceCandidate] = []

        for link in links:
            if not isinstance(link, dict):
                continue

            url = link.get("URL")
            if not url or not isinstance(url, str):
                continue

            content_type = (link.get("content-type") or "").lower()
            content_version = link.get("content-version")
            intended_app = (link.get("intended-application") or "").lower()

            content_is_pdf = "pdf" in content_type or url.lower().endswith(".pdf")
            if not content_is_pdf:
                continue

            if is_staging_url(url):    
                continue

            if url in seen_urls:
                continue
            seen_urls.add(url)

            version_type = _map_content_version(content_version)
            domain = _domain_from_url(url)

            candidates.append(
                SourceCandidate(
                    source_name=self.name,
                    pdf_url=url,
                    landing_page_url=f"https://doi.org/{doi}" if doi else None,
                    version_type=version_type,
                    host_type="publisher" if version_type == "publisher" else "unknown",
                    license=work.get("license", [{}])[0].get("URL") if work.get("license") else None,
                    domain=domain,
                    confidence=base_confidence,
                    is_direct_pdf=url.lower().endswith(".pdf") or "pdf" in content_type,
                    title_match_score=title_match_score,
                    reason="crossref doi lookup" if exact_lookup else "crossref title search",
                    metadata={
                        "doi": doi,
                        "work_type": work_type,
                        "content_version": content_version,
                        "intended_application": intended_app,
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