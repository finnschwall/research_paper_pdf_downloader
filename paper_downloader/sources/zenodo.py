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
class ZenodoSourceProvider:

    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    base_url: str = "https://zenodo.org/api"
    name: str = "zenodo"
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
            records = self._search_by_doi(paper.doi)
            for record in records:
                candidates.extend(
                    self._candidates_from_record(
                        record,
                        exact_lookup=True,
                        title_match_score=1.0 if paper.title else None,
                    )
                )

        if not candidates and self.resolution_config.allow_title_fallback and paper.title:
            records = self._search_by_title(paper.title)
            for record in records:
                record_title = (record.get("metadata") or {}).get("title")
                score = validate_title_match(paper.title, record_title)
                if score < self.resolution_config.title_similarity_threshold:
                    continue

                if paper.year is not None:
                    pub_date = (record.get("metadata") or {}).get("publication_date") or ""
                    if pub_date[:4].isdigit():
                        if abs(int(pub_date[:4]) - paper.year) > 1:
                            continue

                candidates.extend(
                    self._candidates_from_record(
                        record,
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

    def _request_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> dict[str, Any] | list[Any] | None:
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

        if response.status_code == 429:
            return None
        if response.status_code >= 400:
            return None

        try:
            return response.json()
        except ValueError:
            return None

    def _search_by_doi(self, doi: str) -> list[dict[str, Any]]:
        """
        Zenodo supports DOI search via q=doi:"{doi}".
        """
        payload = self._request_json(
            f"{self.base_url}/records",
            params={
                "q": f'doi:"{doi.strip()}"',
                "size": "3",
                "status": "published",
            },
        )
        results = self._extract_hits(payload)

        if not results:
            # Try without quotes for partial match
            payload = self._request_json(
                f"{self.base_url}/records",
                params={
                    "q": f"doi:{doi.strip()}",
                    "size": "3",
                    "status": "published",
                },
            )
            results = self._extract_hits(payload)

        return results

    def _search_by_title(self, title: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            f"{self.base_url}/records",
            params={
                "q": f'title:"{title}"',
                "size": "5",
                "status": "published",
                "type": "publication",
            },
        )
        return self._extract_hits(payload)

    def _extract_hits(
        self, payload: dict[str, Any] | list[Any] | None
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        hits = payload.get("hits") or {}
        items = hits.get("hits") if isinstance(hits, dict) else []
        return [item for item in (items or []) if isinstance(item, dict)]

    def _candidates_from_record(
        self,
        record: dict[str, Any],
        *,
        exact_lookup: bool,
        title_match_score: float | None,
    ) -> list[SourceCandidate]:
        metadata = record.get("metadata") or {}

        
        access_obj = record.get("access") or {}
        files_access = (access_obj.get("files") or "").lower()
        legacy_access_right = (metadata.get("access_right") or "").lower()
        is_open = files_access == "public" or legacy_access_right == "open"
        if not is_open:
            return []

       
        resource_type_obj = metadata.get("resource_type") or {}
        resource_type_id = (resource_type_obj.get("id") or "").lower()
        resource_type_legacy = (resource_type_obj.get("type") or "").lower()
        if resource_type_id.startswith("publication") or resource_type_id.startswith("preprint"):
            resource_type = resource_type_id.split("-")[0]
        elif resource_type_legacy:
            resource_type = resource_type_legacy
        else:
            resource_type = ""

       
        if resource_type not in {"publication", "preprint", ""}:
            files = record.get("files") or []
            pdf_files = [f for f in files if isinstance(f, dict) and
                         f.get("key", "").lower().endswith(".pdf")]
            if not pdf_files:
                return []

        record_doi = metadata.get("doi") or record.get("doi")
        record_title = metadata.get("title")

        
        rights = metadata.get("rights") or metadata.get("license")
        if isinstance(rights, list) and rights:
            license_info = (rights[0] or {}).get("id")
        elif isinstance(rights, dict):
            license_info = rights.get("id")
        else:
            license_info = None

        record_id = record.get("id")
        pub_date = metadata.get("publication_date") or ""

        landing_page_url: str | None = None
        if record_id:
            landing_page_url = f"https://zenodo.org/records/{record_id}"

        
        pdf_url = self._extract_pdf_url(record)
        if not pdf_url:
            return []

        base_confidence = 0.78 if exact_lookup else 0.60

        return [
            SourceCandidate(
                source_name=self.name,
                pdf_url=pdf_url,
                landing_page_url=landing_page_url,
                version_type="accepted",
                host_type="repository",
                license=license_info,
                domain=_domain_from_url(pdf_url),
                confidence=base_confidence,
                is_direct_pdf=True,
                title_match_score=title_match_score,
                reason="zenodo doi lookup" if exact_lookup else "zenodo title search",
                metadata={
                    "zenodo_id": record_id,
                    "work_doi": record_doi,
                    "work_title": record_title,
                    "publication_date": pub_date,
                    "access_right": files_access or legacy_access_right,
                    "resource_type": resource_type,
                },
            )
        ]

    def _extract_pdf_url(self, record: dict[str, Any]) -> str | None:
      
        files: list[dict[str, Any]] = record.get("files") or []

        pdf_files = [
            f for f in files
            if isinstance(f, dict) and f.get("key", "").lower().endswith(".pdf")
        ]

        if not pdf_files and files:
           
            return None

        if not pdf_files:
            return None

        
        pdf_files.sort(key=lambda f: f.get("size", 0), reverse=True)
        best = pdf_files[0]

        links = best.get("links") or {}
        return links.get("self") or links.get("download") or None

    def _deduplicate(self, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
        deduped: dict[str, SourceCandidate] = {}
        for candidate in candidates:
            key = candidate.pdf_url
            existing = deduped.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                deduped[key] = candidate
        return list(deduped.values())