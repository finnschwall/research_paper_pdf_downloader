from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

from paper_downloader.core import host_gate

from paper_downloader.config.models import DownloadConfig, ResolutionConfig
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import SourceCandidate, validate_title_match


_logger = logging.getLogger(__name__)


_PAPER_PAGE_RE = re.compile(
    r'href="(?P<href>/content/(?P<venue>[A-Za-z]+)(?P<year>\d{4})(?:_workshops)?/html/[^"]+_paper\.html)"[^>]*>(?P<title>[^<]+)</a>',
    re.IGNORECASE,
)
_PDF_LINK_RE = re.compile(
    r'href="(?P<href>[^"]+\.pdf)"[^>]*>\s*\[pdf\]\s*</a>',
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


_DOI_YEAR_RE = re.compile(r"\.\b(20\d{2})\.\d+$")


def _clean_html_text(value: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", value)).strip()


def _venue_token_from_venue(venue: str | None) -> str | None:
    if not venue:
        return None
    lowered = venue.lower()
    if "cvpr" in lowered:
        return "CVPR"
    if "iccv" in lowered:
        return "ICCV"
    if "eccv" in lowered:
        return "ECCV"
    if "wacv" in lowered:
        return "WACV"
    return None


def _venue_token_from_doi(doi: str | None) -> str | None:
   
    if not doi:
        return None
    upper = doi.upper()
    if "CVPR" in upper:
        return "CVPR"
    if "ICCV" in upper:
        return "ICCV"
    if "WACV" in upper:
        return "WACV"
    return None


def _year_from_doi(doi: str | None) -> int | None:
    """
    Crossref/IEEE DOIs for CVF papers embed the publication year.
    e.g. 10.1109/CVPR52734.2025.02339 → 2025
    """
    if not doi:
        return None
    match = _DOI_YEAR_RE.search(doi)
    if not match:
        return None
    return int(match.group(1))


@dataclass(slots=True)
class CVFSourceProvider:
    download_config: DownloadConfig
    resolution_config: ResolutionConfig
    name: str = "cvf"

    def resolve(self, paper: PaperRecord) -> list[SourceCandidate]:
        # Venue resolution: try paper.venue first, fall back to DOI
        venue_token = _venue_token_from_venue(paper.venue)
        if not venue_token:
            venue_token = _venue_token_from_doi(paper.doi)

        if not venue_token:
            return []

        if not paper.title:
            return []

        # Year resolution: try paper.year first, fall back to DOI
        year = paper.year
        if not year:
            year = _year_from_doi(paper.doi)

        if not year:
            return []

        index_url = f"https://openaccess.thecvf.com/{venue_token}{year}?day=all"
        index_html = self._fetch_text(index_url)

        if not index_html:
            _logger.debug(
                "cvf: index page unavailable or returned no content | url=%s", index_url
            )
            return []

        matches: list[tuple[str, str, float]] = []
        for match in _PAPER_PAGE_RE.finditer(index_html):
            candidate_title = _clean_html_text(match.group("title"))
            if not candidate_title:
                continue

            score = validate_title_match(paper.title, candidate_title)
            if score < self.resolution_config.title_similarity_threshold:
                continue

            href = match.group("href")
            paper_page_url = f"https://openaccess.thecvf.com{href}"
            matches.append((paper_page_url, candidate_title, score))

        if not matches:
            _logger.debug(
                "cvf: no title match found in index | venue=%s year=%d title=%r",
                venue_token,
                year,
                paper.title,
            )
            return []

        matches.sort(key=lambda item: item[2], reverse=True)

        candidates: list[SourceCandidate] = []
        for paper_page_url, candidate_title, score in matches[:3]:
            pdf_url = self._extract_pdf_from_paper_page(paper_page_url)
            if not pdf_url:
                _logger.debug("cvf: no pdf link found on paper page | url=%s", paper_page_url)
                continue

            candidates.append(
                SourceCandidate(
                    source_name=self.name,
                    pdf_url=pdf_url,
                    landing_page_url=paper_page_url,
                    version_type="publisher",
                    host_type="publisher",
                    license=None,
                    domain="openaccess.thecvf.com",
                    confidence=0.90,
                    is_direct_pdf=True,
                    title_match_score=score,
                    reason="cvf title match",
                    metadata={
                        "matched_title": candidate_title,
                        "cvf_index_url": index_url,
                        "venue_token": venue_token,
                        "year": year,
                        "venue_source": "doi" if not paper.venue else "metadata",
                    },
                )
            )

        return self._deduplicate(candidates)

    def _fetch_text(self, url: str) -> str | None:
        try:
            # A bare `requests.get` rather than a session, because a CVF provider exists
            # per worker thread and a `requests.Session` is not safe to share -- so the
            # host gate is applied here instead of by a GatedSession.
            with host_gate.hold(url):
                response = requests.get(
                    url,
                    headers={
                        "User-Agent": self.download_config.user_agent,
                        "Accept": "text/html,application/xhtml+xml",
                    },
                    timeout=(
                        self.download_config.connect_timeout_seconds,
                        self.download_config.read_timeout_seconds,
                    ),
                    allow_redirects=True,
                    verify=self.download_config.verify_ssl,
                )
        except requests.RequestException as exc:
            _logger.debug("cvf: fetch failed | url=%s | %s", url, exc)
            return None

        if response.status_code >= 400:
            _logger.debug(
                "cvf: fetch returned HTTP %d | url=%s", response.status_code, url
            )
            return None

        return response.text or None

    def _extract_pdf_from_paper_page(self, paper_page_url: str) -> str | None:
        html = self._fetch_text(paper_page_url)
        if not html:
            return None

        match = _PDF_LINK_RE.search(html)
        if not match:
            return None

        href = match.group("href")
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if href.startswith("/"):
            return f"https://openaccess.thecvf.com{href}"
        return f"https://openaccess.thecvf.com/{href.lstrip('/')}"

    def _deduplicate(self, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
        deduped: dict[str, SourceCandidate] = {}
        for candidate in candidates:
            key = candidate.pdf_url
            existing = deduped.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                deduped[key] = candidate
        return list(deduped.values())