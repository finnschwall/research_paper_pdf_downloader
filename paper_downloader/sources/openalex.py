from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests

from paper_downloader.core import host_gate

from paper_downloader.config.models import ApiConfig, DownloadConfig, ResolutionConfig
from paper_downloader.metadata.pmc import (
    europepmc_landing_url,
    europepmc_render_url,
    normalize_pmcid,
    pmcid_from_url,
)
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import SourceCandidate, validate_title_match

#: A location whose only URL is an article page, not a file. Below every direct-PDF
#: confidence: the page still has to be read and its link followed.
_LANDING_ONLY_CONFIDENCE = {True: 0.60, False: 0.45}


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


def _map_version(raw_version: str | None) -> str:
    if not raw_version:
        return "unknown"

    lowered = raw_version.lower()
    if lowered == "publishedversion":
        return "publisher"
    if lowered == "acceptedversion":
        return "accepted"
    if lowered == "submittedversion":
        return "preprint"
    return raw_version


def _map_host_type(source_type: str | None, domain: str | None) -> str:
    lowered = (source_type or "").lower().strip()

    if lowered == "repository":
        return "repository"
    if lowered in {"journal", "conference", "book series", "ebook platform"}:
        return "publisher"

    if domain == "arxiv.org":
        return "preprint"
    if domain in {"aclanthology.org", "openaccess.thecvf.com"}:
        return "publisher"
    if domain in {"pmc.ncbi.nlm.nih.gov", "europepmc.org"}:
        return "repository"

    return "unknown"


@dataclass(slots=True)
class OpenAlexSourceProvider:
    api_config: ApiConfig
    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    base_url: str = "https://api.openalex.org"
    name: str = "openalex"
    _session: requests.Session = field(default=None, init=False, repr=False)  # type: ignore[assignment]
    last_reason: str | None = field(default=None, init=False, repr=False)
    last_failed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._session = host_gate.GatedSession()
        self._session.headers.update(self._headers())

    def __del__(self) -> None:
        if self._session is not None:
            self._session.close()

    def resolve(self, paper: PaperRecord) -> list[SourceCandidate]:
        self.last_reason = None
        self.last_failed = False
        candidates: list[SourceCandidate] = []

        if paper.doi:
            work = self._fetch_work_by_doi(paper.doi)
            if work is None and not self.last_failed:
                self.last_reason = "doi unknown to openalex"
            if work:
                candidates.extend(
                    self._candidates_from_work(
                        paper=paper,
                        work=work,
                        exact_lookup=True,
                        title_match_score=1.0 if paper.title else None,
                    )
                )

        if not candidates and self.resolution_config.allow_title_fallback and paper.title:
            for work in self._search_works_by_title(paper.title):
                work_title = work.get("title") or work.get("display_name")
                match_score = validate_title_match(paper.title, work_title)
                if match_score < self.resolution_config.title_similarity_threshold:
                    continue

                if paper.year is not None:
                    work_year = work.get("publication_year") or work.get("year")
                    if isinstance(work_year, int) and abs(work_year - paper.year) > 1:
                        continue

                candidates.extend(
                    self._candidates_from_work(
                        paper=paper,
                        work=work,
                        exact_lookup=False,
                        title_match_score=match_score,
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

    def _base_params(self) -> dict[str, str]:
        params: dict[str, str] = {
            "include_xpac": "true",
        }
        if self.api_config.openalex_api_key:
            params["api_key"] = self.api_config.openalex_api_key
        return params

    def _request_json(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any] | None:
        merged_params = self._base_params()
        if params:
            merged_params.update(params)

        try:
            response = self._session.get(
                url,
                params=merged_params,
                timeout=self._timeout(),
                allow_redirects=True,
                verify=self.download_config.verify_ssl,
            )
        except requests.RequestException as exc:
            self.last_failed = True
            self.last_reason = f"request failed: {exc.__class__.__name__}"
            return None

        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            self.last_failed = True
            self.last_reason = f"http {response.status_code}"
            return None

        try:
            payload = response.json()
        except ValueError:
            self.last_failed = True
            self.last_reason = "invalid json"
            return None

        return payload if isinstance(payload, dict) else None

    def _fetch_work_by_doi(self, doi: str) -> dict[str, Any] | None:
        normalized = doi.strip().lower()
        doi_url = f"https://doi.org/{normalized}"
        encoded = quote(doi_url, safe=":/")
        url = f"{self.base_url}/works/{encoded}"
        return self._request_json(url)

    def _search_works_by_title(self, title: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            f"{self.base_url}/works",
            params={
                "filter": f"title.search:{title}",
                "per_page": "5",
            },
        )
        if not payload:
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        return [item for item in results if isinstance(item, dict)]

    def _candidates_from_work(
        self,
        *,
        paper: PaperRecord,
        work: dict[str, Any],
        exact_lookup: bool,
        title_match_score: float | None,
    ) -> list[SourceCandidate]:
        base_confidence = 0.95 if exact_lookup else 0.74
        work_title = work.get("title") or work.get("display_name")
        open_access = work.get("open_access") or {}
        work_doi = work.get("doi")
        openalex_id = work.get("id")
        relevance_score = work.get("relevance_score")

        raw_locations: list[dict[str, Any]] = []

        best_oa = work.get("best_oa_location")
        if isinstance(best_oa, dict):
            raw_locations.append(best_oa)

        primary_location = work.get("primary_location")
        if isinstance(primary_location, dict):
            raw_locations.append(primary_location)

        locations = work.get("locations")
        if isinstance(locations, list):
            raw_locations.extend([loc for loc in locations if isinstance(loc, dict)])

        seen_urls: set[str] = set()
        candidates: list[SourceCandidate] = []
        work_is_oa = bool(open_access.get("is_oa")) if isinstance(open_access, dict) else False
        landing_only = 0

        common = {
            "openalex_id": openalex_id,
            "work_title": work_title,
            "work_doi": work_doi,
            "open_access_oa_status": open_access.get("oa_status") if isinstance(open_access, dict) else None,
            "relevance_score": relevance_score,
        }

        def add(candidate: SourceCandidate) -> None:
            if candidate.pdf_url in seen_urls:
                return
            seen_urls.add(candidate.pdf_url)
            candidates.append(candidate)

        # OpenAlex knows the PMCID as a URL under `ids`. PMC's own PDF endpoint needs a
        # browser; Europe PMC serves the same file directly, so every PMCID becomes a
        # Europe PMC candidate whatever the locations say.
        ids = work.get("ids") if isinstance(work.get("ids"), dict) else {}
        pmcid = pmcid_from_url(ids.get("pmcid")) or normalize_pmcid(ids.get("pmcid"))
        if pmcid:
            add(self._europepmc_candidate(
                pmcid, base_confidence, title_match_score=title_match_score,
                exact_lookup=exact_lookup, metadata=common,
            ))

        for location in raw_locations:
            pdf_url = location.get("pdf_url") or location.get("url_for_pdf")
            landing_page_url = location.get("landing_page_url") or location.get("url_for_landing_page")
            location_is_oa = location.get("is_oa")
            asserts_oa = bool(location_is_oa) if location_is_oa is not None else work_is_oa

            if not pdf_url:
                oa_url = open_access.get("oa_url") if isinstance(open_access, dict) else None
                if isinstance(oa_url, str) and oa_url.lower().endswith(".pdf"):
                    pdf_url = oa_url

            # A PMC location, with or without a pdf_url, is best fetched from Europe PMC.
            location_pmcid = pmcid_from_url(pdf_url) or pmcid_from_url(landing_page_url)
            if location_pmcid:
                add(self._europepmc_candidate(
                    location_pmcid, base_confidence, title_match_score=title_match_score,
                    exact_lookup=exact_lookup, metadata=common,
                ))
                continue

            is_direct_pdf = True
            confidence = base_confidence
            if not pdf_url:
                # Landing page only. Still worth a candidate: the page usually carries a
                # citation_pdf_url link the download stage can follow. Not on doi.org,
                # which only bounces to the publisher (publisher_landing does that
                # properly), and not on a denied host.
                if not landing_page_url:
                    continue
                landing_domain = _domain_from_url(landing_page_url)
                if landing_domain in {"doi.org", "dx.doi.org"} or host_gate.is_denied(landing_page_url):
                    continue
                landing_only += 1
                pdf_url = landing_page_url
                is_direct_pdf = False
                confidence = _LANDING_ONLY_CONFIDENCE[exact_lookup]

            source = location.get("source") or {}
            source_type = source.get("type") if isinstance(source, dict) else None
            source_display_name = source.get("display_name") if isinstance(source, dict) else None

            domain = _domain_from_url(pdf_url) or _domain_from_url(landing_page_url)
            version_raw = location.get("version")
            version_type = _map_version(version_raw)
            host_type = _map_host_type(source_type, domain)

            if version_type == "unknown" and host_type == "publisher":
                version_type = "publisher"

            add(
                SourceCandidate(
                    source_name=self.name,
                    pdf_url=pdf_url,
                    landing_page_url=landing_page_url,
                    version_type=version_type,
                    host_type=host_type,
                    license=location.get("license"),
                    domain=domain,
                    confidence=confidence,
                    is_direct_pdf=is_direct_pdf,
                    title_match_score=title_match_score,
                    asserts_open_access=asserts_oa,
                    reason=(
                        ("openalex exact doi lookup" if exact_lookup else "openalex title fallback")
                        + ("" if is_direct_pdf else " (landing page only)")
                    ),
                    metadata={
                        **common,
                        "source_display_name": source_display_name,
                        "source_type": source_type,
                    },
                )
            )

        if not candidates:
            self.last_reason = (
                f"{len(raw_locations)} location(s), none with a usable url"
                if raw_locations else "work has no locations"
            )
        elif landing_only and landing_only == len(candidates):
            self.last_reason = f"{landing_only} location(s), all landing-only"

        return candidates

    def _europepmc_candidate(
        self,
        pmcid: str,
        base_confidence: float,
        *,
        title_match_score: float | None,
        exact_lookup: bool,
        metadata: dict[str, Any],
    ) -> SourceCandidate:
        return SourceCandidate(
            source_name=self.name,
            pdf_url=europepmc_render_url(pmcid),
            landing_page_url=europepmc_landing_url(pmcid),
            version_type="accepted",
            host_type="repository",
            domain="europepmc.org",
            confidence=base_confidence,
            is_direct_pdf=True,
            title_match_score=title_match_score,
            asserts_open_access=True,
            reason="pmcid via openalex" + ("" if exact_lookup else " (title fallback)"),
            metadata={**metadata, "pmcid": f"PMC{pmcid}"},
        )

    def _deduplicate(self, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
        deduped: dict[str, SourceCandidate] = {}
        for candidate in candidates:
            key = candidate.pdf_url
            existing = deduped.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                deduped[key] = candidate
        return list(deduped.values())