from __future__ import annotations

from dataclasses import dataclass, field
import html
import re
from typing import Iterable
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import requests

from paper_downloader.core import host_gate

from paper_downloader.config.models import DownloadConfig, ResolutionConfig
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import SourceCandidate, validate_title_match
from paper_downloader.resolve.resolver import (
    _PUBLISHER_DOMAINS,
    _PREPRINT_DOMAINS,
    _REPOSITORY_DOMAINS,
)


_RESULT_LINK_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_GENERIC_LINK_RE = re.compile(
    r'href="(?P<href>[^"]+\.pdf(?:\?[^"]*)?)"',
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_tags(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", _TAG_RE.sub("", html.unescape(value))).strip()


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    domain = (parsed.netloc or "").lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


def _is_pdf_url(url: str) -> bool:
    lowered = url.lower()
    return ".pdf" in lowered


def _unwrap_duckduckgo_result(url: str) -> str:
    parsed = urlparse(url)
    if "duckduckgo.com" not in (parsed.netloc or "") and "html.duckduckgo.com" not in (parsed.netloc or ""):
        return url

    query = parse_qs(parsed.query)
    uddg = query.get("uddg")
    if uddg:
        return unquote(uddg[0])
    return url


def _is_trusted_domain(domain: str | None, trusted_domains: Iterable[str]) -> bool:
    if not domain:
        return False
    lowered = domain.lower()
    return any(
        lowered == trusted.lower() or lowered.endswith(f".{trusted.lower()}")
        for trusted in trusted_domains
    )


def _infer_host_type(domain: str | None) -> str:
    if not domain:
        return "unknown"
    if domain in _PREPRINT_DOMAINS:
        return "preprint"
    if domain in _PUBLISHER_DOMAINS:
        return "publisher"
    if domain in _REPOSITORY_DOMAINS:
        return "repository"
    return "unknown"


def _infer_version_type(domain: str | None) -> str:
    if not domain:
        return "unknown"
    if domain in _PREPRINT_DOMAINS:
        return "preprint"
    if domain in _PUBLISHER_DOMAINS:
        return "publisher"
    if domain in _REPOSITORY_DOMAINS:
        return "accepted"
    return "unknown"


@dataclass(slots=True)
class BroadSearchSourceProvider:
    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    name: str = "broad_search"
    search_url: str = "https://html.duckduckgo.com/html/"
    max_domains: int = 8
    max_results_per_query: int = 5
    max_pdf_links_from_page: int = 3
    _session: requests.Session = field(default=None, init=False, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._session = host_gate.GatedSession()
        self._session.headers.update(self._headers())

    def __del__(self) -> None:
        if self._session is not None:
            self._session.close()

    def resolve(self, paper: PaperRecord) -> list[SourceCandidate]:
        if not self.resolution_config.allow_title_fallback:
            return []
        if not paper.title:
            return []

        trusted_domains = list(self.resolution_config.trusted_domains)[: self.max_domains]
        candidates: list[SourceCandidate] = []

        for domain in trusted_domains:
            query = self._build_domain_query(paper.title, domain)
            result_html = self._search(query)
            if not result_html:
                continue

            for result in self._parse_results(result_html):
                result_url = _unwrap_duckduckgo_result(result["url"])
                result_title = result["title"]
                result_domain = _domain_from_url(result_url)

                if not _is_trusted_domain(result_domain, trusted_domains):
                    continue

                title_score = validate_title_match(paper.title, result_title)
                if title_score < self.resolution_config.title_similarity_threshold:
                    continue

                if _is_pdf_url(result_url):
                    candidates.append(
                        self._make_candidate(
                            pdf_url=result_url,
                            landing_page_url=result_url,
                            domain=result_domain,
                            title_score=title_score,
                            reason=f"broad domain search direct pdf | {domain}",
                            matched_title=result_title,
                            query=query,
                        )
                    )
                    continue

                page_pdf_links = self._extract_pdf_links_from_page(result_url, trusted_domains)
                for pdf_url in page_pdf_links:
                    pdf_domain = _domain_from_url(pdf_url)
                    if not _is_trusted_domain(pdf_domain, trusted_domains):
                        continue
                    candidates.append(
                        self._make_candidate(
                            pdf_url=pdf_url,
                            landing_page_url=result_url,
                            domain=pdf_domain,
                            title_score=title_score,
                            reason=f"broad domain search landing page -> pdf | {domain}",
                            matched_title=result_title,
                            query=query,
                        )
                    )

        return self._deduplicate(candidates)

    def _build_domain_query(self, title: str, domain: str) -> str:
        return f'site:{domain} "{title}"'

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.download_config.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        }

    def _timeout(self) -> tuple[int, int]:
        return (
            self.download_config.connect_timeout_seconds,
            self.download_config.read_timeout_seconds,
        )

    def _search(self, query: str) -> str | None:
        params = {"q": query}
        try:
            response = self._session.get(
                self.search_url,
                params=params,
                timeout=self._timeout(),
                allow_redirects=True,
                verify=self.download_config.verify_ssl,
            )
        except requests.RequestException:
            return None

        if response.status_code >= 400:
            return None

        return response.text

    def _parse_results(self, html_text: str) -> list[dict[str, str]]:
        parsed: list[dict[str, str]] = []
        for match in _RESULT_LINK_RE.finditer(html_text):
            href = html.unescape(match.group("href")).strip()
            title = _strip_tags(match.group("title"))
            if not href or not title:
                continue
            parsed.append(
                {
                    "url": href,
                    "title": title,
                }
            )
            if len(parsed) >= self.max_results_per_query:
                break
        return parsed

    def _extract_pdf_links_from_page(
        self,
        landing_page_url: str,
        trusted_domains: list[str],
    ) -> list[str]:
        try:
            response = self._session.get(
                landing_page_url,
                timeout=self._timeout(),
                allow_redirects=True,
                verify=self.download_config.verify_ssl,
            )
        except requests.RequestException:
            return []

        if response.status_code >= 400:
            return []

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "html" not in content_type and response.text[:128].lstrip().startswith("%PDF-"):
            return []

        links: list[str] = []
        for match in _GENERIC_LINK_RE.finditer(response.text):
            href = html.unescape(match.group("href")).strip()
            if not href:
                continue
            absolute = urljoin(landing_page_url, href)
            domain = _domain_from_url(absolute)
            if not _is_trusted_domain(domain, trusted_domains):
                continue
            links.append(absolute)
            if len(links) >= self.max_pdf_links_from_page:
                break

        return links

    def _make_candidate(
        self,
        *,
        pdf_url: str,
        landing_page_url: str,
        domain: str | None,
        title_score: float,
        reason: str,
        matched_title: str,
        query: str,
    ) -> SourceCandidate:
        return SourceCandidate(
            source_name=self.name,
            pdf_url=pdf_url,
            landing_page_url=landing_page_url,
            version_type=_infer_version_type(domain),
            host_type=_infer_host_type(domain),
            license=None,
            domain=domain,
            confidence=0.62,
            is_direct_pdf=True,
            title_match_score=title_score,
            reason=reason,
            metadata={
                "matched_title": matched_title,
                "query": query,
            },
        )

    def _deduplicate(self, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
        deduped: dict[str, SourceCandidate] = {}
        for candidate in candidates:
            key = candidate.pdf_url
            existing = deduped.get(key)
            if existing is None or (candidate.title_match_score or 0.0) > (existing.title_match_score or 0.0):
                deduped[key] = candidate
        return list(deduped.values())