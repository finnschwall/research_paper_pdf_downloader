from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

from paper_downloader.config.models import ResolutionConfig
from paper_downloader.core.exceptions import ResolutionError
from paper_downloader.metadata.id_recovery import normalize_title, title_similarity
from paper_downloader.models.paper import PaperRecord


class SourceProvider(Protocol):
    name: str

    def resolve(self, paper: PaperRecord) -> list["SourceCandidate"]:
        ...


@dataclass(slots=True)
class SourceCandidate:
    source_name: str
    pdf_url: str
    landing_page_url: str | None = None
    version_type: str = "unknown"
    host_type: str = "unknown"
    license: str | None = None
    domain: str | None = None
    confidence: float = 0.0
    is_direct_pdf: bool = True
    title_match_score: float | None = None
    reason: str | None = None
    #: Try this only after every other candidate has failed, whatever it scores. Set by
    #: providers whose candidate is a worse *fetch* than a lower-scoring alternative --
    #: `publisher_landing` names the version of record, so it wins on every other term in
    #: `SourceResolver._sort_key`, but a free mirror costs the publisher nothing and is not
    #: behind a bot wall. Also keeps such a candidate from ending the provider search.
    fallback_only: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProviderAttempt:
    source_name: str
    status: str
    candidate_count: int = 0
    message: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResolutionResult:
    paper_key: str
    selected: SourceCandidate
    all_candidates: list[SourceCandidate]
    attempted_sources: list[str]
    provider_attempts: list[ProviderAttempt]

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_key": self.paper_key,
            "selected": self.selected.to_dict(),
            "all_candidates": [candidate.to_dict() for candidate in self.all_candidates],
            "attempted_sources": list(self.attempted_sources),
            "provider_attempts": [attempt.to_dict() for attempt in self.provider_attempts],
        }


class MetadataOpenAccessProvider:
    name = "metadata_open_access"

    def resolve(self, paper: PaperRecord) -> list[SourceCandidate]:
        pdf_url = paper.source_urls.get("open_access_pdf")
        if not pdf_url:
            return []

        domain = _domain_from_url(pdf_url)
        landing_page = paper.source_urls.get("semantic_scholar") or pdf_url

        return [
            SourceCandidate(
                source_name=self.name,
                pdf_url=pdf_url,
                landing_page_url=landing_page,
                version_type=_infer_version_type_from_domain(domain, fallback="unknown"),
                host_type=_infer_host_type_from_domain(domain),
                license=(paper.raw_metadata.get("openAccessPdf") or {}).get("license")
                if isinstance(paper.raw_metadata.get("openAccessPdf"), dict)
                else None,
                domain=domain,
                confidence=0.85,
                is_direct_pdf=pdf_url.lower().endswith(".pdf"),
                reason="metadata openAccessPdf url",
            )
        ]


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    domain = parsed.netloc.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


_PUBLISHER_DOMAINS: frozenset[str] = frozenset({
    "aclanthology.org",
    "openaccess.thecvf.com",
    "dl.acm.org",
    "ieeexplore.ieee.org",
    "link.springer.com",
    "nature.com",
    "sciencedirect.com",
    "tandfonline.com",
    "wiley.com",
    "onlinelibrary.wiley.com",
    "oup.com",
    "academic.oup.com",
    "cambridge.org",
    "journals.sagepub.com",
    "mdpi.com",
    "frontiersin.org",
    "plos.org",
    "journals.plos.org",
    "bmj.com",
    "jamanetwork.com",
    "ahajournals.org",
})

_REPOSITORY_DOMAINS: frozenset[str] = frozenset({
    "pmc.ncbi.nlm.nih.gov",
    "europepmc.org",
    "zenodo.org",
    "core.ac.uk",
    "hal.science",
    "hal.archives-ouvertes.fr",
    "research-repository.uwa.edu.au",
})

_PREPRINT_DOMAINS: frozenset[str] = frozenset({
    "arxiv.org",
    "biorxiv.org",
    "medrxiv.org",
    "chemrxiv.org",
    "ssrn.com",
    "preprints.org",
    "techrxiv.org",
    "osf.io",
})


def _infer_host_type_from_domain(domain: str | None) -> str:
    if not domain:
        return "unknown"
    if domain in _PREPRINT_DOMAINS:
        return "preprint"
    if domain in _PUBLISHER_DOMAINS:
        return "publisher"
    if domain in _REPOSITORY_DOMAINS:
        return "repository"
    return "unknown"


def _infer_version_type_from_domain(domain: str | None, *, fallback: str = "unknown") -> str:
    if not domain:
        return fallback
    if domain in _PREPRINT_DOMAINS:
        return "preprint"
    if domain in _PUBLISHER_DOMAINS:
        return "publisher"
    if domain in _REPOSITORY_DOMAINS:
        return "accepted"   
    return fallback


class SourceResolver:
    def __init__(
        self,
        config: ResolutionConfig,
        *,
        providers: Iterable[SourceProvider] | None = None,
    ) -> None:
        self.config = config
        self.providers = list(providers) if providers is not None else [
            MetadataOpenAccessProvider(),
        ]

    def resolve(self, paper: PaperRecord) -> ResolutionResult:
        attempted_sources: list[str] = []
        provider_attempts: list[ProviderAttempt] = []
        candidates: list[SourceCandidate] = []

        for provider in self.providers:
            attempted_sources.append(provider.name)

            try:
                provider_candidates = provider.resolve(paper)
            except Exception as exc:
                provider_attempts.append(
                    ProviderAttempt(
                        source_name=provider.name,
                        status="failed",
                        candidate_count=0,
                        error=str(exc),
                    )
                )
                continue

            provider_attempts.append(
                ProviderAttempt(
                    source_name=provider.name,
                    status="candidates_found" if provider_candidates else "no_candidates",
                    candidate_count=len(provider_candidates),
                    message="candidates discovered" if provider_candidates else "no candidate returned",
                )
            )

            for candidate in provider_candidates:
                if not candidate.domain:
                    candidate.domain = _domain_from_url(candidate.pdf_url)
                prior_confidence = candidate.confidence
                candidate.confidence = self._score_candidate(
                    paper, candidate, prior_confidence
                )
                candidates.append(candidate)

            if self._good_enough(candidates):
                break

        if not candidates:
            error = ResolutionError(
                f"No downloadable source candidate found for paper: {paper.paper_key}"
            )
            setattr(error, "provider_attempts", [attempt.to_dict() for attempt in provider_attempts])
            raise error

        seen: dict[str, SourceCandidate] = {}
        for c in candidates:
            existing = seen.get(c.pdf_url)
            if existing is None or c.confidence > existing.confidence:
                seen[c.pdf_url] = c
        candidates = list(seen.values())
            
        ranked = sorted(candidates, key=self._sort_key, reverse=True)
        selected = ranked[0]
        return ResolutionResult(
            paper_key=paper.paper_key,
            selected=selected,
            all_candidates=ranked,
            attempted_sources=attempted_sources,
            provider_attempts=provider_attempts,
        )

    def _good_enough(self, candidates: list[SourceCandidate]) -> bool:
        """Should we stop asking further providers?

        Yes once some candidate is a direct link to a PDF, on a host we trust, scoring at
        or above the threshold -- a later provider can at best point at the same file
        somewhere else.

        When prefer_publisher_version is on we additionally insist the candidate *is* the
        publisher version, because that preference is precisely a reason to keep looking:
        stopping at a preprint would quietly override the setting. That makes this a
        no-op for preprint-only papers, which is the right trade -- the remaining
        providers cost about a second, and correctness is worth more than that.
        """
        if not self.config.stop_when_confident:
            return False
        threshold = self.config.stop_confidence_threshold
        for candidate in candidates:
            if candidate.fallback_only:
                # A last-resort candidate is never a reason to stop looking -- finding a
                # free copy is the whole point of the providers still to come.
                continue
            if not candidate.is_direct_pdf or candidate.confidence < threshold:
                continue
            if not self._is_trusted_domain(candidate.domain):
                continue
            if self.config.prefer_publisher_version and candidate.version_type != "publisher":
                continue
            return True
        return False

    def _sort_key(self, candidate: SourceCandidate) -> tuple[int, int, int, int, float]:
        return (
            0 if candidate.fallback_only else 1,
            1 if candidate.version_type == "publisher" else 0,
            1 if candidate.is_direct_pdf else 0,
            1 if candidate.host_type == "publisher" else 0,
            candidate.confidence,
        )

    def _score_candidate(
        self,
        paper: PaperRecord,
        candidate: SourceCandidate,
        prior_confidence: float,
    ) -> float:
        """
        Compute a normalised quality score for a candidate.
        """
        score = 0.0

        if candidate.is_direct_pdf:
            score += 0.20

        if self._is_trusted_domain(candidate.domain):
            score += 0.20

        if self.config.prefer_publisher_version and candidate.version_type == "publisher":
            score += 0.30
        elif candidate.version_type == "publisher":
            score += 0.15
        elif candidate.version_type == "accepted":
            score += 0.20

        if candidate.version_type == "preprint":
            if self.config.allow_preprints:
                score += 0.10
            else:
                score -= 1.00

        if candidate.host_type == "publisher":
            score += 0.10
        elif candidate.host_type == "repository":
            score += 0.05

        if candidate.title_match_score is not None:
            if candidate.title_match_score >= self.config.title_similarity_threshold:
                score += 0.10
            else:
                score -= 0.20

        score += max(0.0, min(prior_confidence, 1.0)) * 0.10
        return round(score, 6)

    def _is_trusted_domain(self, domain: str | None) -> bool:
        if not domain:
            return False
        domain = domain.lower()
        return any(
            domain == trusted or domain.endswith(f".{trusted}")
            for trusted in self.config.trusted_domains
        )


def validate_title_match(
    paper_title: str | None,
    candidate_title: str | None,
) -> float:
    if not paper_title or not candidate_title:
        return 0.0
    if normalize_title(paper_title) == normalize_title(candidate_title):
        return 1.0
    return title_similarity(paper_title, candidate_title)