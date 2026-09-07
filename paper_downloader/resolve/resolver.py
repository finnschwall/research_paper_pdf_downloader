from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

from paper_downloader.config.models import ResolutionConfig
from paper_downloader.core import host_gate
from paper_downloader.core.exceptions import ResolutionError
from paper_downloader.metadata.id_recovery import normalize_title, title_similarity
from paper_downloader.metadata.pmc import (
    europepmc_landing_url,
    europepmc_render_url,
    pmcid_from_url,
)
from paper_downloader.models.paper import PaperRecord


class SourceProvider(Protocol):
    """One place to ask "is there a copy of this paper?".

    Optionally a provider also exposes ``last_reason`` (why the last ``resolve`` returned
    what it did -- "http 422: email required", "3 locations, all landing-only") and
    ``last_failed`` (True when it was an HTTP or network failure rather than an empty
    answer). The resolver reads both with ``getattr`` and defaults, so a provider that
    does not keep them is still a provider.
    """
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
    #: The provider that produced this candidate says a free copy of the paper exists --
    #: Unpaywall's `is_oa`, OpenAlex's `is_oa`, a DOAJ listing, Europe PMC's `inEPMC`. When
    #: every such candidate then fails to download, the paper is "open access but this
    #: client could not fetch it", which is a different fact from "not free" and must be
    #: stored differently. See `DownloadPipelineResult.oa_asserted_by`.
    asserts_open_access: bool = False
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
    #: The provider's own account of why it returned what it did. `status` alone cannot
    #: tell "Unpaywall is switched off" from "Unpaywall has never heard of this DOI".
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResolutionResult:
    paper_key: str
    #: The best candidate, or None for a `resolve_more` round that found nothing new.
    #: `resolve` itself never returns None here -- it raises instead.
    selected: SourceCandidate | None
    all_candidates: list[SourceCandidate]
    attempted_sources: list[str]
    provider_attempts: list[ProviderAttempt]

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_key": self.paper_key,
            "selected": self.selected.to_dict() if self.selected is not None else None,
            "all_candidates": [candidate.to_dict() for candidate in self.all_candidates],
            "attempted_sources": list(self.attempted_sources),
            "provider_attempts": [attempt.to_dict() for attempt in self.provider_attempts],
        }


class MetadataOpenAccessProvider:
    """Semantic Scholar's `openAccessPdf.url`, when it is something this client can fetch.

    That field is often not a PDF at all. Three cases are handled here rather than handed
    to the download stage as-is:

    * A `doi.org` URL. Fetching it directly is wrong for the reasons in
      `sources/publisher.py` -- the redirect chain is rate-limited under doi.org rather
      than the publisher, and the User-Agent is chosen for the wrong host. The
      `publisher_landing` provider resolves the same DOI to the publisher's real host, so
      the URL is skipped here and nothing is lost.
    * A PMC article page. PMC's PDF endpoint needs JavaScript; Europe PMC serves the same
      file directly, so the candidate is rewritten to the Europe PMC render URL.
    * Any other non-`.pdf` URL is kept as a landing page (`is_direct_pdf=False`, low
      confidence) so the landing-page step can look for the link, and so it never ends the
      provider search by itself.
    """
    name = "metadata_open_access"

    def __init__(self) -> None:
        self.last_reason: str | None = None
        self.last_failed = False

    def resolve(self, paper: PaperRecord) -> list[SourceCandidate]:
        self.last_reason = None
        self.last_failed = False
        pdf_url = paper.source_urls.get("open_access_pdf")
        if not pdf_url:
            self.last_reason = "record has no openAccessPdf url"
            return []

        domain = _domain_from_url(pdf_url)
        landing_page = paper.source_urls.get("semantic_scholar") or pdf_url
        license_ = (
            (paper.raw_metadata.get("openAccessPdf") or {}).get("license")
            if isinstance(paper.raw_metadata.get("openAccessPdf"), dict)
            else None
        )

        pmcid = pmcid_from_url(pdf_url)
        if pmcid:
            return [
                SourceCandidate(
                    source_name=self.name,
                    pdf_url=europepmc_render_url(pmcid),
                    landing_page_url=europepmc_landing_url(pmcid),
                    version_type="accepted",
                    host_type="repository",
                    license=license_,
                    domain="europepmc.org",
                    confidence=0.85,
                    is_direct_pdf=True,
                    asserts_open_access=True,
                    reason="pmcid via metadata openAccessPdf url",
                    metadata={"pmcid": f"PMC{pmcid}", "original_url": pdf_url},
                )
            ]

        if domain in {"doi.org", "dx.doi.org"}:
            self.last_reason = "openAccessPdf url is a doi.org redirect; left to publisher_landing"
            return []

        is_direct_pdf = pdf_url.lower().endswith(".pdf")
        return [
            SourceCandidate(
                source_name=self.name,
                pdf_url=pdf_url,
                landing_page_url=landing_page,
                version_type=_infer_version_type_from_domain(domain, fallback="unknown"),
                host_type=_infer_host_type_from_domain(domain),
                license=license_,
                domain=domain,
                confidence=0.85 if is_direct_pdf else 0.55,
                is_direct_pdf=is_direct_pdf,
                reason="metadata openAccessPdf url" if is_direct_pdf
                else "metadata openAccessPdf url (landing page, not a file)",
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
        """Ask providers in order until one is good enough; raise if none has anything.

        This is the first round. When every candidate it returns later fails to download,
        the orchestrator asks `resolve_more` for the providers this round never reached.
        """
        candidates, attempts, asked = self._ask(paper, self.providers)

        if not candidates:
            error = ResolutionError(
                f"No downloadable source candidate found for paper: {paper.paper_key}"
            )
            setattr(error, "provider_attempts", [attempt.to_dict() for attempt in attempts])
            raise error

        ranked = self._rank(candidates)
        return ResolutionResult(
            paper_key=paper.paper_key,
            selected=ranked[0],
            all_candidates=ranked,
            attempted_sources=asked,
            provider_attempts=attempts,
        )

    def resolve_more(
        self, paper: PaperRecord, *, already_asked: list[str]
    ) -> ResolutionResult | None:
        """Ask the providers not yet asked for this paper; None when none are left.

        Exists because the first round stops at the first "good enough" candidate, and a
        good-enough candidate can still 403. Before this, that 403 ended the paper even
        though Europe PMC, three providers further down, had a free copy on record.

        The same early stop applies within a round, so a paper costs one more provider per
        round rather than the whole remaining chain. `all_candidates` is empty and
        `selected` None when the round found nothing; the attempts are still reported.
        """
        asked_set = set(already_asked)
        remaining = [p for p in self.providers if p.name not in asked_set]
        if not remaining:
            return None

        candidates, attempts, asked = self._ask(paper, remaining)
        ranked = self._rank(candidates)
        return ResolutionResult(
            paper_key=paper.paper_key,
            selected=ranked[0] if ranked else None,
            all_candidates=ranked,
            attempted_sources=asked,
            provider_attempts=attempts,
        )

    def _ask(
        self, paper: PaperRecord, providers: list[SourceProvider]
    ) -> tuple[list[SourceCandidate], list[ProviderAttempt], list[str]]:
        """Run `providers` in order with the early stop. Returns (candidates, attempts, asked)."""
        attempted_sources: list[str] = []
        provider_attempts: list[ProviderAttempt] = []
        candidates: list[SourceCandidate] = []

        for provider in providers:
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
                        reason=str(exc),
                    )
                )
                continue

            reason = getattr(provider, "last_reason", None)
            failed = bool(getattr(provider, "last_failed", False)) and not provider_candidates
            if provider_candidates:
                status, message = "candidates_found", "candidates discovered"
            elif failed:
                status, message = "failed", "provider request failed"
            else:
                status, message = "no_candidates", "no candidate returned"
            provider_attempts.append(
                ProviderAttempt(
                    source_name=provider.name,
                    status=status,
                    candidate_count=len(provider_candidates),
                    message=message,
                    error=reason if failed else None,
                    reason=reason,
                )
            )

            for candidate in provider_candidates:
                if not candidate.domain:
                    candidate.domain = _domain_from_url(candidate.pdf_url)
                prior_confidence = candidate.confidence
                candidate.confidence = self._score_candidate(
                    paper, candidate, prior_confidence
                )
                if host_gate.is_denied(candidate.pdf_url):
                    # Kept, not dropped: a paper whose only copies are on denied hosts
                    # must fail in the download stage as host-blocked (retryable), not
                    # here as "no candidate" (permanent). But it may not end the search.
                    candidate.fallback_only = True
                candidates.append(candidate)

            if self._good_enough(candidates):
                break

        return candidates, provider_attempts, attempted_sources

    def _rank(self, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
        seen: dict[str, SourceCandidate] = {}
        for c in candidates:
            existing = seen.get(c.pdf_url)
            if existing is None or c.confidence > existing.confidence:
                seen[c.pdf_url] = c
        return sorted(seen.values(), key=self._sort_key, reverse=True)

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

        A candidate on a host that is denied, or currently blocked for refusing us, is
        never good enough either: it is going to fail without a request, so stopping on
        it would end the search for exactly the papers that most need the rest of it.
        """
        if not self.config.stop_when_confident:
            return False
        threshold = self.config.stop_confidence_threshold
        for candidate in candidates:
            if candidate.fallback_only:
                # A last-resort candidate is never a reason to stop looking -- finding a
                # free copy is the whole point of the providers still to come.
                continue
            if host_gate.is_denied(candidate.pdf_url) or host_gate.block_reason(candidate.pdf_url):
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