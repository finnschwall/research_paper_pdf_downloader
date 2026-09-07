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


@dataclass(slots=True)
class UnpaywallSourceProvider:
    api_config: ApiConfig
    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    base_url: str = "https://api.unpaywall.org/v2"
    name: str = "unpaywall"
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
        if not self.api_config.unpaywall_email:
            # Unpaywall answers HTTP 422 without a contact address, so the provider is
            # off. Said here, because "no_candidates" would read as "no free copy".
            self.last_reason = "unpaywall email not configured; provider is off"
            return []

        candidates: list[SourceCandidate] = []

        if paper.doi:
            payload = self._lookup_by_doi(paper.doi)
            if payload is None and not self.last_failed:
                self.last_reason = "doi unknown to unpaywall"
            if payload:
                candidates.extend(
                    self._candidates_from_payload(
                        payload=payload,
                        exact_lookup=True,
                        title_match_score=1.0 if paper.title else None,
                    )
                )

        if not candidates and self.resolution_config.allow_title_fallback and paper.title:
            for result in self._search_by_title(paper.title):
                response = result.get("response")
                if not isinstance(response, dict):
                    continue

                response_title = response.get("title")
                match_score = validate_title_match(paper.title, response_title)
                if match_score < self.resolution_config.title_similarity_threshold:
                    continue

                if paper.year is not None:
                    result_year = response.get("year")
                    if isinstance(result_year, int) and abs(result_year - paper.year) > 1:
                        continue

                candidates.extend(
                    self._candidates_from_payload(
                        payload=response,
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

    def _request_json(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any] | None:
        merged = {"email": self.api_config.unpaywall_email}
        if params:
            merged.update(params)

        try:
            response = self._session.get(
                url,
                params=merged,
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

    def _lookup_by_doi(self, doi: str) -> dict[str, Any] | None:
        encoded = quote(doi.strip(), safe="")
        return self._request_json(f"{self.base_url}/{encoded}")

    def _search_by_title(self, title: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            f"{self.base_url}/search/",
            params={
                "query": title,
                "is_oa": "true",
                "page": "1",
            },
        )
        if not payload:
            return []

        results = payload.get("results")
        if not isinstance(results, list):
            return []

        filtered: list[dict[str, Any]] = []
        for item in results[:5]:
            if isinstance(item, dict):
                filtered.append(item)
        return filtered

    def _candidates_from_payload(
        self,
        *,
        payload: dict[str, Any],
        exact_lookup: bool,
        title_match_score: float | None,
    ) -> list[SourceCandidate]:
        if payload.get("is_oa") is False:
            self.last_reason = "unpaywall says is_oa false"
            return []

        base_confidence = 0.88 if exact_lookup else 0.68
        title = payload.get("title")
        doi = payload.get("doi")
        oa_status = payload.get("oa_status")

        raw_locations: list[dict[str, Any]] = []
        best_oa = payload.get("best_oa_location")
        if isinstance(best_oa, dict):
            raw_locations.append(best_oa)

        locations = payload.get("oa_locations")
        if isinstance(locations, list):
            raw_locations.extend([loc for loc in locations if isinstance(loc, dict)])

        seen_urls: set[str] = set()
        candidates: list[SourceCandidate] = []
        landing_only = 0
        common = {"work_title": title, "work_doi": doi, "oa_status": oa_status}

        def add(candidate: SourceCandidate) -> None:
            if candidate.pdf_url in seen_urls:
                return
            seen_urls.add(candidate.pdf_url)
            candidates.append(candidate)

        for location in raw_locations:
            pdf_url = location.get("url_for_pdf")
            landing_page_url = location.get("url_for_landing_page") or location.get("url")
            evidence = location.get("evidence")

            # PMC's PDF endpoint needs a browser; Europe PMC serves the same file.
            pmcid = pmcid_from_url(pdf_url) or pmcid_from_url(landing_page_url)
            if pmcid:
                add(SourceCandidate(
                    source_name=self.name,
                    pdf_url=europepmc_render_url(pmcid),
                    landing_page_url=europepmc_landing_url(pmcid),
                    version_type=_map_version(location.get("version")) if location.get("version") else "accepted",
                    host_type="repository",
                    license=location.get("license"),
                    domain="europepmc.org",
                    confidence=base_confidence,
                    is_direct_pdf=True,
                    title_match_score=title_match_score,
                    asserts_open_access=True,
                    reason="pmcid via unpaywall",
                    metadata={**common, "evidence": evidence, "pmcid": f"PMC{pmcid}"},
                ))
                continue

            is_direct_pdf = True
            confidence = base_confidence
            if not pdf_url:
                # Landing page only -- still a candidate, because the page usually carries
                # the PDF link. Not on doi.org (publisher_landing resolves that properly)
                # and not on a denied host.
                if not landing_page_url:
                    continue
                landing_domain = _domain_from_url(landing_page_url)
                if landing_domain in {"doi.org", "dx.doi.org"} or host_gate.is_denied(landing_page_url):
                    continue
                landing_only += 1
                pdf_url = landing_page_url
                is_direct_pdf = False
                confidence = _LANDING_ONLY_CONFIDENCE[exact_lookup]

            domain = _domain_from_url(pdf_url) or _domain_from_url(landing_page_url)
            host_type = location.get("host_type") or "unknown"
            version_type = _map_version(location.get("version"))

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
                    # Every oa_location Unpaywall lists is its claim that a free copy exists.
                    asserts_open_access=True,
                    reason=(
                        ("unpaywall exact doi lookup" if exact_lookup else "unpaywall title fallback")
                        + ("" if is_direct_pdf else " (landing page only)")
                    ),
                    metadata={**common, "evidence": evidence},
                )
            )

        if not candidates:
            self.last_reason = (
                f"{len(raw_locations)} oa_location(s), none with a usable url"
                if raw_locations else "is_oa but no oa_locations"
            )
        elif landing_only and landing_only == len(candidates):
            self.last_reason = f"{landing_only} oa_location(s), all landing-only"

        return candidates

    def _deduplicate(self, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
        deduped: dict[str, SourceCandidate] = {}
        for candidate in candidates:
            key = candidate.pdf_url
            existing = deduped.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                deduped[key] = candidate
        return list(deduped.values())