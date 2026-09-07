from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
class EuropePMCSourceProvider:
    """Europe PMC REST API.

    A record is usable when the full text is *in* Europe PMC with a PDF (`inEPMC` and
    `hasPDF`), not when it is flagged open access. `isOpenAccess` is the licence subset:
    author manuscripts deposited under a funder mandate are `isOpenAccess N` yet served
    freely at the render URL. Gating on the flag dropped exactly those -- a measured
    3.2 MB PDF reported as "no candidates".
    """

    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    base_url: str = "https://www.ebi.ac.uk/europepmc/webservices/rest"
    name: str = "europepmc"
    _session: requests.Session = field(default=None, init=False, repr=False)  # type: ignore[assignment]
    last_reason: str | None = field(default=None, init=False, repr=False)
    last_failed: bool = field(default=False, init=False, repr=False)
    _records_seen: int = field(default=0, init=False, repr=False)

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
        self._records_seen = 0

        if paper.pmcid:
            results = self._search(f"PMCID:PMC{paper.pmcid}")
            candidates.extend(self._candidates_from_results(results, exact_lookup=True, paper=paper))

        if not candidates and paper.pmid:
            results = self._search(f"EXT_ID:{paper.pmid} AND SRC:MED")
            candidates.extend(self._candidates_from_results(results, exact_lookup=True, paper=paper))

        if not candidates and paper.doi:
            results = self._search(f"DOI:{paper.doi}")
            candidates.extend(self._candidates_from_results(results, exact_lookup=True, paper=paper))

        if not candidates and self.resolution_config.allow_title_fallback and paper.title:
            results = self._search(
                f'TITLE:"{paper.title}"',
                extra_params={"resultType": "lite"},
            )
            for article in results:
                article_title = article.get("title")
                score = validate_title_match(paper.title, article_title)
                if score < self.resolution_config.title_similarity_threshold:
                    continue
                if paper.year is not None:
                    pub_year = article.get("pubYear")
                    if isinstance(pub_year, str) and pub_year.isdigit():
                        if abs(int(pub_year) - paper.year) > 1:
                            continue
                candidates.extend(
                    self._candidates_from_article(
                        article, exact_lookup=False, title_match_score=score
                    )
                )

        if not candidates and self.last_reason is None:
            self.last_reason = (
                f"{self._records_seen} matching record(s), none in Europe PMC with a PDF"
                if self._records_seen else "no matching record"
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

    def _search(
        self,
        query: str,
        *,
        extra_params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": "5",
        }
        if extra_params:
            params.update(extra_params)

        try:
            response = self._session.get(
                f"{self.base_url}/search",
                params=params,
                timeout=self._timeout(),
                allow_redirects=True,
                verify=self.download_config.verify_ssl,
            )
        except requests.RequestException as exc:
            self.last_failed = True
            self.last_reason = f"request failed: {exc.__class__.__name__}"
            return []

        if response.status_code >= 400:
            self.last_failed = True
            self.last_reason = f"http {response.status_code}"
            return []

        try:
            payload = response.json()
        except ValueError:
            self.last_failed = True
            self.last_reason = "invalid json"
            return []

        if not isinstance(payload, dict):
            return []

        result_list = payload.get("resultList") or {}
        results = [r for r in (result_list.get("result") or []) if isinstance(r, dict)]
        self._records_seen += len(results)
        return results

    def _candidates_from_results(
        self,
        results: list[dict[str, Any]],
        *,
        exact_lookup: bool,
        paper: PaperRecord,
    ) -> list[SourceCandidate]:
        candidates: list[SourceCandidate] = []
        for article in results:
            title_match_score: float | None = None
            if paper.title:
                article_title = article.get("title")
                score = validate_title_match(paper.title, article_title)
                if score < self.resolution_config.title_similarity_threshold and not exact_lookup:
                    continue
                title_match_score = score if article_title else None
            candidates.extend(
                self._candidates_from_article(
                    article,
                    exact_lookup=exact_lookup,
                    title_match_score=title_match_score,
                )
            )
        return candidates

    def _candidates_from_article(
        self,
        article: dict[str, Any],
        *,
        exact_lookup: bool,
        title_match_score: float | None,
    ) -> list[SourceCandidate]:
        in_epmc = str(article.get("inEPMC") or "").upper() == "Y"
        has_pdf = str(article.get("hasPDF") or "").upper() == "Y"
        if not (in_epmc and has_pdf):
            return []
        # The OA subset carries the publisher's version under an open licence; the rest of
        # what is in EPMC is an author manuscript. Only `prefer_publisher_version` scoring
        # depends on this -- both download the same way.
        is_oa = str(article.get("isOpenAccess") or "").upper() == "Y"

        pmcid_raw = article.get("pmcid") or ""
        pmid = article.get("pmid") or article.get("extId")
        doi = article.get("doi")
        article_title = article.get("title")
        pub_year = article.get("pubYear")
        source = article.get("source")

        # Strip leading "PMC" prefix correctly — removeprefix is exact, lstrip is not
        pmcid = pmcid_raw.upper().removeprefix("PMC") if pmcid_raw else None

        pdf_url = self._extract_pdf_url(article, pmcid=pmcid)
        if not pdf_url:
            return []

        landing_page_url: str | None = None
        if pmcid:
            landing_page_url = f"https://europepmc.org/articles/PMC{pmcid}"
        elif pmid:
            landing_page_url = f"https://europepmc.org/article/MED/{pmid}"

        base_confidence = 0.87 if exact_lookup else 0.67

        return [
            SourceCandidate(
                source_name=self.name,
                pdf_url=pdf_url,
                landing_page_url=landing_page_url,
                version_type="publisher" if is_oa else "accepted",
                host_type="repository",
                license=article.get("license"),
                domain=_domain_from_url(pdf_url),
                confidence=base_confidence,
                is_direct_pdf=pdf_url.lower().endswith(".pdf") or "pdf" in pdf_url.lower(),
                title_match_score=title_match_score,
                # In Europe PMC with a PDF *is* the assertion that a free copy exists.
                asserts_open_access=True,
                reason="europepmc exact id lookup" if exact_lookup else "europepmc title search",
                metadata={
                    "pmcid": f"PMC{pmcid}" if pmcid else None,
                    "pmid": pmid,
                    "doi": doi,
                    "work_title": article_title,
                    "pub_year": pub_year,
                    "source": source,
                    "is_open_access": is_oa,
                },
            )
        ]

    def _extract_pdf_url(
        self,
        article: dict[str, Any],
        *,
        pmcid: str | None,
    ) -> str | None:
        
        full_text_url_list = article.get("fullTextUrlList") or {}
        full_text_urls = full_text_url_list.get("fullTextUrl") or []

        for entry in full_text_urls:
            if not isinstance(entry, dict):
                continue
            style = (entry.get("documentStyle") or "").lower()
            availability = (entry.get("availabilityCode") or "").upper()
            url = entry.get("url")
            if style == "pdf" and availability in {"OA", "F"} and url:
                return url

        # Canonical Europe PMC PDF render URL — current documented format
        if pmcid:
            return f"https://europepmc.org/articles/PMC{pmcid}?pdf=render"

        return None

    def _deduplicate(self, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
        deduped: dict[str, SourceCandidate] = {}
        for candidate in candidates:
            key = candidate.pdf_url
            existing = deduped.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                deduped[key] = candidate
        return list(deduped.values())