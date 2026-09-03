from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from paper_downloader.core import host_gate

from paper_downloader.config.models import ApiConfig, DownloadConfig
from paper_downloader.core.exceptions import MetadataError
from paper_downloader.metadata.id_recovery import build_paper_key, recover_identifiers_from_record
from paper_downloader.models.paper import PaperRecord


DEFAULT_PAPER_FIELDS = [
    "paperId",
    "corpusId",
    "externalIds",
    "url",
    "title",
    "abstract",
    "venue",
    "year",
    "publicationVenue",
    "publicationDate",
    "authors",
    "isOpenAccess",
    "openAccessPdf",
    "citationCount",
    "publicationTypes",
]

_SS_MAX_RETRIES = 3
_SS_RETRY_BASE_SECONDS = 2.0


@dataclass(slots=True)
class SemanticScholarClient:
    api_config: ApiConfig
    download_config: DownloadConfig
    base_url: str = "https://api.semanticscholar.org/graph/v1"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.download_config.user_agent,
        }
        if self.api_config.semantic_scholar_api_key:
            headers["x-api-key"] = self.api_config.semantic_scholar_api_key
        return headers

    def _timeout(self) -> tuple[int, int]:
        return (
            self.download_config.connect_timeout_seconds,
            self.download_config.read_timeout_seconds,
        )

    def _session(self) -> requests.Session:
        session = host_gate.GatedSession()
        session.headers.update(self._headers())
        return session

    def fetch_paper_by_id(
        self,
        paper_id: str,
        *,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        selected_fields = fields or DEFAULT_PAPER_FIELDS
        url = f"{self.base_url}/paper/{paper_id}"
        params = {"fields": ",".join(selected_fields)}

        last_exc: Exception | None = None

        for attempt in range(1, _SS_MAX_RETRIES + 2):
            try:
                with self._session() as session:
                    response = session.get(
                        url,
                        params=params,
                        timeout=self._timeout(),
                        allow_redirects=True,
                        verify=self.download_config.verify_ssl,
                    )
            except requests.RequestException as exc:
                raise MetadataError(
                    f"Semantic Scholar request failed for paper id: {paper_id}"
                ) from exc

            if response.status_code == 429:
                if attempt > _SS_MAX_RETRIES:
                    break
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() \
                    else _SS_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                last_exc = MetadataError(
                    f"Semantic Scholar returned HTTP 429 for paper id: {paper_id}"
                )
                time.sleep(wait)
                continue

            if response.status_code >= 400:
                raise MetadataError(
                    f"Semantic Scholar returned HTTP {response.status_code} for paper id: {paper_id}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise MetadataError(
                    f"Invalid JSON returned for paper id: {paper_id}"
                ) from exc

            if not isinstance(payload, dict):
                raise MetadataError(
                    f"Unexpected response payload for paper id: {paper_id}"
                )

            return payload

        raise last_exc or MetadataError(
            f"Semantic Scholar request failed after {_SS_MAX_RETRIES} retries for paper id: {paper_id}"
        )

    def fetch_papers_by_ids(
        self,
        paper_ids: list[str],
        *,
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not paper_ids:
            return []

        selected_fields = fields or DEFAULT_PAPER_FIELDS
        url = f"{self.base_url}/paper/batch"
        body = {
            "ids": paper_ids,
            "fields": selected_fields,
        }

        last_exc: Exception | None = None

        for attempt in range(1, _SS_MAX_RETRIES + 2):
            try:
                with self._session() as session:
                    response = session.post(
                        url,
                        json=body,
                        timeout=self._timeout(),
                        allow_redirects=True,
                        verify=self.download_config.verify_ssl,
                    )
            except requests.RequestException as exc:
                raise MetadataError("Semantic Scholar batch request failed") from exc

            if response.status_code == 429:
                if attempt > _SS_MAX_RETRIES:
                    break
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() \
                    else _SS_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                last_exc = MetadataError("Semantic Scholar batch returned HTTP 429")
                time.sleep(wait)
                continue

            if response.status_code >= 400:
                raise MetadataError(
                    f"Semantic Scholar batch returned HTTP {response.status_code}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise MetadataError(
                    "Invalid batch JSON returned from Semantic Scholar"
                ) from exc

            if not isinstance(payload, list):
                raise MetadataError(
                    "Unexpected batch response payload from Semantic Scholar"
                )

            return [item for item in payload if isinstance(item, dict)]

        raise last_exc or MetadataError(
            f"Semantic Scholar batch request failed after {_SS_MAX_RETRIES} retries"
        )

    def enrich_paper_record(
        self,
        paper: PaperRecord,
        *,
        fields: list[str] | None = None,
    ) -> PaperRecord:
        if paper.raw_metadata and paper.title:
            return paper

        paper_id = self._build_ss_paper_id(paper)
        if not paper_id:
            raise MetadataError(
                f"No Semantic Scholar identifier available to enrich paper: {paper.paper_key}"
            )

        payload = self.fetch_paper_by_id(paper_id, fields=fields)
        ids = recover_identifiers_from_record(payload)

        rebuilt_key = build_paper_key(
            doi=ids.get("doi") or paper.doi,
            arxiv_id=ids.get("arxiv_id") or paper.arxiv_id,
            semantic_scholar_paper_id=ids.get("semantic_scholar_paper_id") or paper.semantic_scholar_paper_id,
            corpus_id=ids.get("corpus_id") or paper.corpus_id,
            title=payload.get("title") or paper.title,
        )

        return PaperRecord.from_semantic_scholar_record(
            payload,
            paper_key=rebuilt_key,
            input_type=paper.input_type,
            input_value=paper.input_value,
            doi=ids.get("doi") or paper.doi,
            arxiv_id=ids.get("arxiv_id") or paper.arxiv_id,
            dblp_id=ids.get("dblp_id") or paper.dblp_id,
            acl_id=ids.get("acl_id") or paper.acl_id,
            pmid=ids.get("pmid") or paper.pmid,
            corpus_id=ids.get("corpus_id") or paper.corpus_id,
        )

    def _build_ss_paper_id(self, paper: PaperRecord) -> str | None:
        if paper.semantic_scholar_paper_id:
            return paper.semantic_scholar_paper_id
        if paper.corpus_id:
            return f"CorpusId:{paper.corpus_id}"
        if paper.doi:
            return f"DOI:{paper.doi}"
        if paper.arxiv_id:
            return f"ARXIV:{paper.arxiv_id}"
        if paper.acl_id:
            return f"ACL:{paper.acl_id}"
        if paper.pmid:
            return f"PMID:{paper.pmid}"
        if paper.pmcid:
            return f"PMCID:{paper.pmcid}"
        if paper.mag_id:
            return f"MAG:{paper.mag_id}"
        return None