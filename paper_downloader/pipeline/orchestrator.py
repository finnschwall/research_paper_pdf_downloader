from __future__ import annotations

from dataclasses import dataclass, field, fields as dc_fields
import logging
from pathlib import Path
from typing import Any, Callable

from paper_downloader.config.models import PipelineConfig
from paper_downloader.core import host_gate
from paper_downloader.core.exceptions import (
    ConfigurationError,
    DownloadError,
    HostBlockedError,
    MetadataError,
    NotAPDFError,
    ResolutionError,
)
from paper_downloader.core.stages import PipelineStage
from paper_downloader.download.downloader import PDFDownloader, DownloadResult
from paper_downloader.download.landing_page import extract_pdf_url
from paper_downloader.inputs.parser import parse_inputs
from paper_downloader.metadata.pmc import (
    europepmc_render_url,
    is_pmc_interstitial,
    pmcid_from_url,
)
from paper_downloader.metadata.semantic_scholar import SemanticScholarClient
from paper_downloader.models.manifest import PipelineManifest
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import (
    MetadataOpenAccessProvider,
    ResolutionResult,
    SourceCandidate,
    ProviderAttempt,
    SourceResolver,
)
from paper_downloader.sources.acl import ACLSourceProvider
from paper_downloader.sources.arxiv import ArxivSourceProvider
from paper_downloader.sources.broad_search import BroadSearchSourceProvider
from paper_downloader.sources.core import CORESourceProvider
from paper_downloader.sources.crossref import CrossrefSourceProvider
from paper_downloader.sources.cvf import CVFSourceProvider
from paper_downloader.sources.doaj import DOAJSourceProvider
from paper_downloader.sources.europepmc import EuropePMCSourceProvider
from paper_downloader.sources.openalex import OpenAlexSourceProvider
from paper_downloader.sources.publisher import PublisherLandingSourceProvider
from paper_downloader.sources.unpaywall import UnpaywallSourceProvider
from paper_downloader.sources.zenodo import ZenodoSourceProvider
from paper_downloader.state.manifest_store import ManifestStore
from paper_downloader.state.status import BatchStatus, StageStatus
from paper_downloader.storage.paths import PathResolver
from paper_downloader.storage.writers import write_json


def _safe_construct(cls: type, data: dict[str, Any]) -> Any:
    valid_keys = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid_keys})


@dataclass(slots=True)
class DownloadPipelineResult:
    paper_key: str
    original_paper_key: str
    input_type: str
    input_value: str | None
    semantic_scholar_id: str | None
    title: str | None
    downloaded: bool
    reused_existing: bool
    pdf_path: str | None
    status: str
    manifest_path: str | None
    selected_source: dict[str, Any] = field(default_factory=dict)
    provider_attempts: list[dict[str, Any]] = field(default_factory=list)
    download_attempts: list[dict[str, Any]] = field(default_factory=list)
    failure_stage: str | None = None
    failure_code: str | None = None
    error: str | None = None
    #: Providers that said a free copy of this paper exists (see
    #: `SourceCandidate.asserts_open_access`). Non-empty on a failure means "open access
    #: but this client could not fetch it", which a caller must keep apart from "not free".
    oa_asserted_by: list[str] = field(default_factory=list)
    #: Why the paper has no PDF, in one word a caller can branch on. One of
    #: FAILURE_REASONS, or None on success.
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_key": self.paper_key,
            "original_paper_key": self.original_paper_key,
            "input_type": self.input_type,
            "input_value": self.input_value,
            "semantic_scholar_id": self.semantic_scholar_id,
            "title": self.title,
            "downloaded": self.downloaded,
            "reused_existing": self.reused_existing,
            "pdf_path": self.pdf_path,
            "status": self.status,
            "manifest_path": self.manifest_path,
            "selected_source": self.selected_source,
            "provider_attempts": self.provider_attempts,
            "download_attempts": self.download_attempts,
            "failure_stage": self.failure_stage,
            "failure_code": self.failure_code,
            "error": self.error,
            "oa_asserted_by": list(self.oa_asserted_by),
            "failure_reason": self.failure_reason,
        }


#: The vocabulary of `DownloadPipelineResult.failure_reason`, most to least decisive when
#: several attempts say different things (see `_failure_reason`).
FAILURE_REASONS = (
    "host_refused_client",   # a host turned this client away; nothing learned about the paper
    "host_denied",           # a host on the configured deny list was never asked
    "transient",             # 5xx, 429, network error, metadata lookup -- try again later
    "page_without_link",     # reached an article page whose PDF needs a browser
    "withdrawn",             # the arXiv copy was withdrawn
    "not_free",              # 401/403 on the article itself, or no candidate anywhere
    "dead_link",             # every URL on record answers 404 or the like
)


class DownloadOrchestrator:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        paths: PathResolver | None = None,
        manifest_store: ManifestStore | None = None,
        metadata_client: SemanticScholarClient | None = None,
        resolver: SourceResolver | None = None,
        downloader: PDFDownloader | None = None,
    ) -> None:
        self.config = config
        self.paths = paths or PathResolver(config.output)
        self.paths.ensure_base_dirs()
        self.manifest_store = manifest_store or ManifestStore(self.paths)
        self.metadata_client = metadata_client or SemanticScholarClient(
            config.apis, config.download
        )
        self.downloader = downloader or PDFDownloader(config.download)
        self.logger = logging.getLogger("paper_downloader")

        # Both are process-wide facts, so they live in host_gate rather than on this
        # object: every downloader and every provider session in the process must agree
        # on which hosts are off limits and which have refused us lately.
        host_gate.set_denied_hosts(config.download.denied_hosts)
        host_gate.configure_persistence(Path(config.output.root_dir) / "host_refusals.json")

        if resolver is None:
            resolver = self._build_resolver(config)
        self.resolver = resolver

    def close(self) -> None:
        """Release the downloader's HTTP session. Safe to call more than once."""
        closer = getattr(self.downloader, "close", None)
        if callable(closer):
            closer()

    @staticmethod
    def _build_resolver(config: PipelineConfig) -> SourceResolver:
        """Build the provider chain named by `resolution.source_priority`.

        The config key already existed but was ignored, so turning a provider off meant
        editing this function. It is now the enable list *and* the order: drop a name and
        that provider is never constructed, so it costs nothing. Names not listed here are
        ignored with a warning rather than failing the run -- `venue_exact` appears in the
        shipped default and has no implementation.
        """
        builders: dict[str, Callable[[], Any]] = {
            "metadata_open_access": MetadataOpenAccessProvider,
            "acl": ACLSourceProvider,
            "cvf": lambda: CVFSourceProvider(config.download, config.resolution),
            "arxiv": lambda: ArxivSourceProvider(config.download, config.resolution),
            "openalex": lambda: OpenAlexSourceProvider(config.apis, config.download, config.resolution),
            "unpaywall": lambda: UnpaywallSourceProvider(config.apis, config.download, config.resolution),
            "europepmc": lambda: EuropePMCSourceProvider(config.download, config.resolution),
            "crossref": lambda: CrossrefSourceProvider(config.apis, config.download, config.resolution),
            "core": lambda: CORESourceProvider(config.apis, config.download, config.resolution),
            "zenodo": lambda: ZenodoSourceProvider(config.download, config.resolution),
            "doaj": lambda: DOAJSourceProvider(config.download, config.resolution),
            "broad_search": lambda: BroadSearchSourceProvider(config.download, config.resolution),
            "publisher_landing": lambda: PublisherLandingSourceProvider(
                config.apis, config.download, config.resolution
            ),
        }

        logger = logging.getLogger("paper_downloader")
        providers = []
        for name in config.resolution.source_priority:
            builder = builders.get(name)
            if builder is None:
                logger.warning("resolution.source_priority names unknown provider %r -- skipped", name)
                continue
            providers.append(builder())

        if not providers:
            raise ConfigurationError(
                "resolution.source_priority named no known providers; nothing to resolve with"
            )
        return SourceResolver(config.resolution, providers=providers)

    def process_inputs(
        self, raw_input: str | Path | dict[str, Any] | list[Any]
    ) -> list[DownloadPipelineResult]:
        papers = parse_inputs(raw_input)
        total = len(papers)
        results: list[DownloadPipelineResult] = []

        for index, paper in enumerate(papers, start=1):
            self._log(index, total, self._ref(paper), "starting")
            try:
                results.append(self._process_paper(paper, index=index, total=total))
            except Exception as exc:
                self._log(
                    index, total, self._ref(paper),
                    f"failed before manifest: {exc}",
                    level="error",
                )
                results.append(
                    DownloadPipelineResult(
                        paper_key=paper.paper_key,
                        original_paper_key=paper.paper_key,
                        input_type=paper.input_type,
                        input_value=paper.input_value,
                        semantic_scholar_id=paper.semantic_scholar_paper_id,
                        title=paper.title,
                        downloaded=False,
                        reused_existing=False,
                        pdf_path=None,
                        status="failed_pipeline_error",
                        manifest_path=None,
                        failure_code="pipeline_error",
                        error=str(exc),
                    )
                )

        return results

    def _process_paper(
        self, paper: PaperRecord, *, index: int, total: int
    ) -> DownloadPipelineResult:
        manifest: PipelineManifest | None = None
        working_paper = paper

        try:
            working_paper, metadata_fetched = self._prepare_paper(paper, index=index, total=total)
            if working_paper.paper_key != paper.paper_key:
                self._log(
                    index, total, self._ref(paper),
                    f"normalized to {working_paper.paper_key}",
                )

            manifest = self.manifest_store.get_or_create(working_paper)
            self.manifest_store.update_paper_snapshot(manifest, working_paper)
            self._persist_metadata_snapshot(working_paper)

            self.manifest_store.update_stage(
                manifest, PipelineStage.PARSE_INPUT, StageStatus.SUCCEEDED,
                message="input parsed",
            )
            self._log(index, total, self._ref(paper), "input parsed")

            self._handle_metadata_stage(manifest, metadata_fetched, index=index, total=total, paper=paper)
            self._handle_identifier_stage(manifest, working_paper, index=index, total=total)

            resolution = self._handle_resolution_stage(manifest, working_paper, index=index, total=total)
            download_result = self._handle_download_stage(
                manifest, resolution, index=index, total=total, paper=paper,
                resolve_paper=working_paper,
            )

            manifest.batch_status = BatchStatus.PARTIAL_SUCCESS
            self.manifest_store.save(manifest)

            if download_result.reused_existing:
                self._log(
                    index, total, self._ref(paper),
                    f"pdf already exists, skipping download | path={download_result.output_path}",
                )
            else:
                self._log(
                    index, total, self._ref(paper),
                    f"downloaded | path={download_result.output_path}",
                )

            return DownloadPipelineResult(
                paper_key=working_paper.paper_key,
                original_paper_key=paper.paper_key,
                input_type=paper.input_type,
                input_value=paper.input_value,
                semantic_scholar_id=working_paper.semantic_scholar_paper_id,
                title=working_paper.title,
                downloaded=True,
                reused_existing=download_result.reused_existing,
                pdf_path=download_result.output_path,
                status="already_exists" if download_result.reused_existing else "downloaded",
                manifest_path=str(self.manifest_store.path_for(working_paper.paper_key)),
                selected_source=dict(manifest.selected_source or {}),
                provider_attempts=list(
                    (manifest.stats.get("resolution") or {}).get("provider_attempts") or []
                ),
                download_attempts=list(manifest.stats.get("download_attempts") or []),
            )

        except Exception as exc:
            failure_code = self._failure_code_from_exception(exc)
            failure_stage = manifest.failed_stage if manifest is not None else None

            if manifest is not None and manifest.batch_status != BatchStatus.FAILED:
                manifest.batch_status = BatchStatus.FAILED
                manifest.final_error = str(exc)
                self.manifest_store.save(manifest)

            self._log(
                index, total, self._ref(paper),
                f"failed | code={failure_code} | {exc}",
                level="error",
            )

            provider_attempts: list[dict[str, Any]] = []
            download_attempts: list[dict[str, Any]] = []
            selected_source: dict[str, Any] = {}

            if manifest is not None:
                resolution_stats = manifest.stats.get("resolution") or {}
                provider_attempts = list(resolution_stats.get("provider_attempts") or [])
                download_attempts = list(manifest.stats.get("download_attempts") or [])
                selected_source = dict(manifest.selected_source or {})

            if isinstance(exc, ResolutionError):
                provider_attempts = provider_attempts or list(
                    getattr(exc, "provider_attempts", []) or []
                )
            if isinstance(exc, DownloadError):
                download_attempts = download_attempts or list(
                    getattr(exc, "download_attempts", []) or []
                )

            oa_asserted_by = self._oa_asserted_by(manifest)
            failure_reason = self._failure_reason(failure_code, download_attempts, provider_attempts)
            if manifest is not None:
                manifest.stats["failure_reason"] = failure_reason
                manifest.stats["oa_asserted_by"] = oa_asserted_by
                self.manifest_store.save(manifest)

            return DownloadPipelineResult(
                paper_key=working_paper.paper_key,
                original_paper_key=paper.paper_key,
                input_type=paper.input_type,
                input_value=paper.input_value,
                semantic_scholar_id=working_paper.semantic_scholar_paper_id,
                title=working_paper.title,
                downloaded=False,
                reused_existing=False,
                pdf_path=None,
                status=f"failed_{failure_code}",
                manifest_path=str(self.manifest_store.path_for(working_paper.paper_key))
                if manifest is not None
                else None,
                selected_source=selected_source,
                provider_attempts=provider_attempts,
                download_attempts=download_attempts,
                failure_stage=failure_stage,
                failure_code=failure_code,
                error=str(exc),
                oa_asserted_by=oa_asserted_by,
                failure_reason=failure_reason,
            )

    @staticmethod
    def _oa_asserted_by(manifest: PipelineManifest | None) -> list[str]:
        """Providers whose candidates carried an open-access assertion, in chain order."""
        if manifest is None:
            return []
        resolution_stats = manifest.stats.get("resolution") or {}
        names: list[str] = []
        for candidate in resolution_stats.get("all_candidates") or []:
            if isinstance(candidate, dict) and candidate.get("asserts_open_access"):
                name = candidate.get("source_name")
                if name and name not in names:
                    names.append(name)
        return names

    @staticmethod
    def _failure_reason(
        failure_code: str,
        attempts: list[dict[str, Any]],
        provider_attempts: list[dict[str, Any]],
    ) -> str:
        """One word for why there is no PDF, read off what actually happened.

        Several attempts may disagree; the order in FAILURE_REASONS decides. A host turning
        us away outranks everything because it says nothing about the paper; a transient
        error outranks a permanent-looking one because a re-run might succeed; a page that
        needs a browser outranks "not free" because a person could still get it.
        """
        if failure_code == "unresolved_no_legal_pdf":
            return "not_free"
        if failure_code in ("metadata_fetch_failed", "resolution_failed", "pipeline_error"):
            return "transient"
        if not attempts:
            return "transient"

        found: set[str] = set()
        for a in attempts:
            status = a.get("http_status")
            if a.get("host_blocked"):
                found.add("host_denied" if a.get("host_denied") else "host_refused_client")
            elif status is not None and (status >= 500 or status == 429):
                found.add("transient")
            elif a.get("not_a_pdf"):
                found.add("page_without_link")
            elif status == 404 and a.get("source_name") == "arxiv":
                # arXiv answers 404 for exactly one thing: an id whose PDF was withdrawn.
                found.add("withdrawn")
            elif status in (401, 403):
                found.add("not_free")
            elif status is not None:
                found.add("dead_link")
            else:
                # No status and not a page: the request never completed.
                found.add("transient")

        if any("withdrawn" in str(pa.get("reason") or "") for pa in provider_attempts
               if pa.get("source_name") == "arxiv"):
            found.add("withdrawn")

        for reason in FAILURE_REASONS:
            if reason in found:
                return reason
        return "transient"

    def _prepare_paper(
        self, paper: PaperRecord, *, index: int, total: int
    ) -> tuple[PaperRecord, bool]:
        if paper.raw_metadata and paper.title:
            return paper, False

        if paper.input_type in {
            "semantic_scholar_paper_id",
            "corpus_id",
            "doi",
            "arxiv",
            "acl_id",
            "pmid",
            "pmcid",
            "mag",
        }:
            self._log(index, total, self._ref(paper), "fetching metadata")
            enriched = self.metadata_client.enrich_paper_record(paper)
            return enriched, True

        return paper, False

    def _persist_metadata_snapshot(self, paper: PaperRecord) -> None:
        payload = paper.raw_metadata if paper.raw_metadata else paper.to_dict()
        write_json(self.paths.metadata_path(paper.paper_key), payload)

    def _handle_metadata_stage(
        self,
        manifest: PipelineManifest,
        metadata_fetched: bool,
        *,
        index: int,
        total: int,
        paper: PaperRecord,
    ) -> None:
        if self._is_stage_completed(manifest, PipelineStage.FETCH_METADATA):
            return
        if metadata_fetched:
            self.manifest_store.update_stage(
                manifest, PipelineStage.FETCH_METADATA, StageStatus.SUCCEEDED,
                message="metadata fetched",
            )
            self._log(index, total, self._ref(paper), "metadata fetched")
        else:
            self.manifest_store.update_stage(
                manifest, PipelineStage.FETCH_METADATA, StageStatus.SKIPPED,
                message="metadata fetch not required",
            )
            self._log(index, total, self._ref(paper), "metadata fetch skipped")

    def _handle_identifier_stage(
        self,
        manifest: PipelineManifest,
        paper: PaperRecord,
        *,
        index: int,
        total: int,
    ) -> None:
        if self._is_stage_completed(manifest, PipelineStage.RECOVER_IDENTIFIERS):
            return

        self.manifest_store.update_paper_snapshot(manifest, paper)
        self.manifest_store.update_stage(
            manifest,
            PipelineStage.RECOVER_IDENTIFIERS,
            StageStatus.SUCCEEDED,
            message="identifiers normalized",
            details={
                "doi": paper.doi,
                "arxiv_id": paper.arxiv_id,
                "semantic_scholar_paper_id": paper.semantic_scholar_paper_id,
                "corpus_id": paper.corpus_id,
            },
        )

        parts = []
        if paper.doi:
            parts.append(f"doi={paper.doi}")
        if paper.arxiv_id:
            parts.append(f"arxiv_id={paper.arxiv_id}")
        if paper.semantic_scholar_paper_id:
            parts.append(f"ss_id={paper.semantic_scholar_paper_id}")
        if paper.corpus_id:
            parts.append(f"corpus_id={paper.corpus_id}")

        msg = "identifiers normalized"
        if parts:
            msg = f"{msg} | " + " | ".join(parts)
        self._log(index, total, self._ref(paper), msg)

    def _can_reuse_resolution(self, manifest: PipelineManifest) -> bool:
        """May this paper skip resolution and reuse the candidate list on its manifest?

        Only while that list has not already been tried and lost. Resume exists for the run
        that was interrupted before downloading; once the download stage has failed, every
        candidate in the list is a proven dead URL and replaying it fails the same way. It
        is also how a paper stays permanently unavailable after a new source provider is
        added, because the provider chain never runs for it again. Retrying a failed paper
        has to mean resolving it afresh.
        """
        if not self._is_stage_completed(manifest, PipelineStage.RESOLVE_SOURCE):
            return False
        if not manifest.selected_source:
            return False
        download = manifest.get_stage_state(PipelineStage.DOWNLOAD_PDF)
        return download.status != StageStatus.FAILED

    def _handle_resolution_stage(
        self,
        manifest: PipelineManifest,
        paper: PaperRecord,
        *,
        index: int,
        total: int,
    ) -> ResolutionResult:
        if self._can_reuse_resolution(manifest):
            resolution_stats = manifest.stats.get("resolution") or {}
            selected = manifest.selected_source

            provider_attempts = [
                _safe_construct(ProviderAttempt, a)
                for a in list(resolution_stats.get("provider_attempts") or [])
            ]
            all_candidates_raw = list(resolution_stats.get("all_candidates") or [])
            all_candidates = (
                [_safe_construct(SourceCandidate, c) for c in all_candidates_raw]
                if all_candidates_raw
                else [_safe_construct(SourceCandidate, selected)]
            )

            return ResolutionResult(
                paper_key=paper.paper_key,
                selected=_safe_construct(SourceCandidate, selected),
                all_candidates=all_candidates,
                attempted_sources=list(resolution_stats.get("attempted_sources") or []),
                provider_attempts=provider_attempts,
            )

        self.manifest_store.update_stage(
            manifest, PipelineStage.RESOLVE_SOURCE, StageStatus.IN_PROGRESS,
            message="resolving source", increment_attempt=True,
        )
        self._log(index, total, self._ref(paper), "resolving source")

        try:
            resolution = self.resolver.resolve(paper)
            manifest.stats["resolution"] = resolution.to_dict()
            self.manifest_store.update_selected_source(manifest, resolution.selected.to_dict())
            self.manifest_store.save(manifest)

            self._log_provider_attempts(
                [a.to_dict() for a in resolution.provider_attempts], index=index, total=total, paper=paper,
            )

            self.manifest_store.update_stage(
                manifest, PipelineStage.RESOLVE_SOURCE, StageStatus.SUCCEEDED,
                message="source resolved",
                details={
                    "selected_source_name": resolution.selected.source_name,
                    "selected_domain": resolution.selected.domain,
                    "attempted_sources": resolution.attempted_sources,
                    "candidate_count": len(resolution.all_candidates),
                },
            )
            self._log(
                index, total, self._ref(paper),
                f"selected source: {resolution.selected.source_name} | {resolution.selected.pdf_url}",
            )
            return resolution

        except Exception as exc:
            provider_attempts = list(getattr(exc, "provider_attempts", []) or [])
            manifest.stats["resolution"] = {"provider_attempts": provider_attempts}
            self.manifest_store.save(manifest)

            self._log_provider_attempts(provider_attempts, index=index, total=total, paper=paper)

            self.manifest_store.update_stage(
                manifest, PipelineStage.RESOLVE_SOURCE, StageStatus.FAILED, error=str(exc),
            )
            raise ResolutionError(str(exc)) from exc

    def _log_provider_attempts(
        self, attempts: list[dict[str, Any]], *, index: int, total: int, paper: PaperRecord,
    ) -> None:
        for attempt in attempts:
            source_name = attempt.get("source_name")
            status = attempt.get("status")
            count = attempt.get("candidate_count", 0)
            reason = attempt.get("reason") or attempt.get("error")
            suffix = f" | {reason}" if reason else ""
            if status == "failed":
                self._log(index, total, self._ref(paper), f"{source_name} failed{suffix}")
            elif status == "no_candidates":
                self._log(index, total, self._ref(paper), f"{source_name} returned no candidates{suffix}")
            else:
                self._log(index, total, self._ref(paper), f"{source_name} returned {count} candidate(s){suffix}")

    def _record_resolution_round(self, manifest: PipelineManifest, more: ResolutionResult) -> None:
        """Append a `resolve_more` round to the manifest's resolution record."""
        stats = manifest.stats.get("resolution")
        if not isinstance(stats, dict):
            stats = {}
            manifest.stats["resolution"] = stats
        stats.setdefault("provider_attempts", []).extend(a.to_dict() for a in more.provider_attempts)
        stats.setdefault("attempted_sources", []).extend(more.attempted_sources)
        stats.setdefault("all_candidates", []).extend(c.to_dict() for c in more.all_candidates)
        stats["rounds"] = int(stats.get("rounds") or 1) + 1

    def _handle_download_stage(
        self,
        manifest: PipelineManifest,
        resolution: ResolutionResult,
        *,
        index: int,
        total: int,
        paper: PaperRecord,
        resolve_paper: PaperRecord | None = None,
    ) -> DownloadResult:
        """Try every candidate; when all fail, ask the providers not yet asked, and repeat.

        Resolution stops at the first "good enough" candidate, and that candidate can 403.
        Before this the 403 ended the paper: 12 of 109 failed papers in one production run
        had stopped at an OpenAlex or Unpaywall URL that then failed, with Europe PMC three
        providers further down holding a free copy nobody asked for. Now the chain resumes
        (`SourceResolver.resolve_more`) until a download succeeds or no provider is left;
        only then is it a `DownloadError`, and every attempt of every round is on the
        manifest.

        `paper` is the input record, used for log references; `resolve_paper` is the
        record after metadata enrichment, which is what the providers need.
        """
        output_pdf_path = manifest.output_paths["pdf"]
        resolve_paper = resolve_paper or paper

        if self._is_stage_completed(manifest, PipelineStage.DOWNLOAD_PDF) and Path(output_pdf_path).exists():
            download_stats = manifest.stats.get("download")
            if isinstance(download_stats, dict):
                selected_url = resolution.selected.pdf_url if resolution.selected else ""
                return DownloadResult(
                    url=download_stats.get("url") or selected_url,
                    final_url=download_stats.get("final_url") or selected_url,
                    output_path=download_stats.get("output_path") or output_pdf_path,
                    content_type=download_stats.get("content_type"),
                    size_bytes=int(download_stats.get("size_bytes") or 0),
                    sha256=download_stats.get("sha256") or "",
                    reused_existing=True,
                )

        self.manifest_store.update_stage(
            manifest, PipelineStage.DOWNLOAD_PDF, StageStatus.IN_PROGRESS,
            message="downloading pdf", increment_attempt=True,
        )

        attempts: list[dict[str, Any]] = []
        last_error: str | None = None
        tried_urls: set[str] = set()
        asked = list(resolution.attempted_sources)
        candidates = list(resolution.all_candidates)
        attempt_no = 0
        round_no = 1

        while True:
            for candidate in candidates:
                if candidate.pdf_url in tried_urls:
                    continue
                tried_urls.add(candidate.pdf_url)
                attempt_no += 1
                self._log(
                    index, total, self._ref(paper),
                    f"trying candidate {attempt_no} (round {round_no}) | {candidate.source_name} | {candidate.pdf_url}",
                )
                attempted_url = candidate.pdf_url
                try:
                    try:
                        result = self.downloader.download(
                            candidate.pdf_url,
                            output_pdf_path,
                            skip_if_valid=self.config.resume.verify_existing_files,
                        )
                    except NotAPDFError as exc:
                        result, attempted_url = self._retry_via_landing_page(
                            exc, candidate, output_pdf_path, index=index, total=total, paper=paper,
                        )
                    attempts.append({
                        "candidate_index": attempt_no,
                        "round": round_no,
                        "source_name": candidate.source_name,
                        "pdf_url": attempted_url,
                        "status": "succeeded",
                        "error": None,
                        "result": result.to_dict(),
                    })
                    manifest.stats["download"] = result.to_dict()
                    manifest.stats["download_attempts"] = attempts
                    self.manifest_store.update_selected_source(manifest, candidate.to_dict())
                    self.manifest_store.save(manifest)

                    self.manifest_store.update_stage(
                        manifest, PipelineStage.DOWNLOAD_PDF, StageStatus.SUCCEEDED,
                        message="pdf downloaded",
                        details={
                            **result.to_dict(),
                            "selected_source_name": candidate.source_name,
                            "candidate_index": attempt_no,
                        },
                    )
                    self._log(index, total, self._ref(paper), f"pdf downloaded | source={candidate.source_name}")
                    return result

                except Exception as exc:
                    last_error = str(exc)
                    attempts.append({
                        "candidate_index": attempt_no,
                        "round": round_no,
                        "source_name": candidate.source_name,
                        "pdf_url": attempted_url,
                        "status": "failed",
                        "error": str(exc),
                        "http_status": getattr(exc, "status_code", None),
                        "not_a_pdf": isinstance(exc, NotAPDFError),
                        # Whether the host turned this server away rather than answering
                        # about the paper. A caller deciding "is this paper worth another
                        # attempt" cannot tell from the status alone -- see host_gate.
                        "host_blocked": isinstance(exc, HostBlockedError),
                        # ...and whether that was the deny list rather than an observed
                        # refusal. The caller's stored detail must not confuse the two.
                        "host_denied": bool(getattr(exc, "denied", False)),
                        "host": getattr(exc, "host", "") or host_gate.host_key(attempted_url),
                    })
                    manifest.stats["download_attempts"] = attempts
                    self.manifest_store.save(manifest)
                    self._log(index, total, self._ref(paper), f"{candidate.source_name} download failed | {exc}")

            # Every candidate so far has failed. Ask the providers the early stop skipped.
            more = self.resolver.resolve_more(resolve_paper, already_asked=asked)
            if more is None:
                break
            round_no += 1
            asked.extend(name for name in more.attempted_sources if name not in asked)
            self._record_resolution_round(manifest, more)
            self.manifest_store.save(manifest)
            self._log_provider_attempts(
                [a.to_dict() for a in more.provider_attempts], index=index, total=total, paper=paper,
            )
            candidates = [c for c in more.all_candidates if c.pdf_url not in tried_urls]
            if candidates:
                self._log(
                    index, total, self._ref(paper),
                    f"all candidates failed | {len(candidates)} more from round {round_no}",
                )

        summary_error = last_error or "all download candidates failed"
        self.manifest_store.update_stage(
            manifest, PipelineStage.DOWNLOAD_PDF, StageStatus.FAILED, error=summary_error,
        )
        error = DownloadError(summary_error)
        setattr(error, "download_attempts", attempts)
        raise error

    def _retry_via_landing_page(
        self,
        exc: NotAPDFError,
        candidate: SourceCandidate,
        output_pdf_path: str,
        *,
        index: int,
        total: int,
        paper: PaperRecord,
    ) -> tuple[DownloadResult, str]:
        """The URL served a web page. Look for the PDF link on it and try that, once.

        Most gold-OA DOIs resolve to an article page rather than a file, so without this a
        freely available paper is reported as undownloadable. One hop only, and only to a
        link the page itself offers: no crawling, no guessing at URL patterns.

        One exception to "only what the page offers": PMC's PDF endpoint answers with a
        small "Preparing to download" page that fetches the file from JavaScript. That is
        not a page without a PDF, it is a PDF this client cannot reach at this host --
        Europe PMC serves the same file directly, so the request is rewritten there.

        Re-raises the original NotAPDFError when the page offers nothing, so the caller
        still sees the real reason and moves on to the next candidate.
        """
        if not self.config.download.landing_page_fallback:
            raise exc

        rewritten = self._pmc_rewrite(exc)
        if rewritten:
            self._log(
                index, total, self._ref(paper),
                f"{candidate.source_name} hit PMC's download interstitial | fetching from Europe PMC | {rewritten}",
            )
            result = self.downloader.download(
                rewritten, output_pdf_path, skip_if_valid=self.config.resume.verify_existing_files,
            )
            return result, rewritten

        landing_url = exc.final_url or candidate.pdf_url
        pdf_url = extract_pdf_url(exc.body, landing_url)
        if not pdf_url or pdf_url == candidate.pdf_url:
            # Before concluding "this page offers no PDF", check whether it is a page at
            # all. Cloudflare and Incapsula serve their interstitials with a 200 and no
            # download link, which is indistinguishable from a paywalled article stub
            # unless you look at the boilerplate.
            if host_gate.note_challenge_page(landing_url, exc.body):
                raise HostBlockedError(
                    f"{host_gate.host_key(landing_url)} served a bot challenge instead of "
                    f"the article page: {landing_url}",
                    host=host_gate.host_key(landing_url),
                ) from exc
            raise exc

        self._log(
            index, total, self._ref(paper),
            f"{candidate.source_name} served a landing page | following its PDF link | {pdf_url}",
        )
        try:
            result = self.downloader.download(
                pdf_url,
                output_pdf_path,
                skip_if_valid=self.config.resume.verify_existing_files,
                referer=landing_url,
            )
        except NotAPDFError as second:
            # The page's own PDF link may be the PMC interstitial (a PMC article page
            # links to its /pdf/ endpoint). Same rewrite, one more hop.
            rewritten = self._pmc_rewrite(second)
            if not rewritten:
                raise
            self._log(
                index, total, self._ref(paper),
                f"{candidate.source_name}'s PDF link is PMC's download interstitial | fetching from Europe PMC | {rewritten}",
            )
            result = self.downloader.download(
                rewritten, output_pdf_path, skip_if_valid=self.config.resume.verify_existing_files,
            )
            return result, rewritten
        return result, pdf_url

    @staticmethod
    def _pmc_rewrite(exc: NotAPDFError) -> str | None:
        """The Europe PMC render URL for a PMC interstitial, or None if this is not one."""
        if not is_pmc_interstitial(exc.final_url, exc.body):
            return None
        pmcid = pmcid_from_url(exc.final_url)
        return europepmc_render_url(pmcid) if pmcid else None

    def _is_stage_completed(self, manifest: PipelineManifest, stage: PipelineStage) -> bool:
        if not self.config.resume.enabled or not self.config.resume.skip_completed_stages:
            return False
        return manifest.get_stage_state(stage).status == StageStatus.SUCCEEDED

    def _failure_code_from_exception(self, exc: Exception) -> str:
        if isinstance(exc, MetadataError):
            return "metadata_fetch_failed"
        if isinstance(exc, ResolutionError):
            if "No downloadable source candidate found" in str(exc):
                return "unresolved_no_legal_pdf"
            return "resolution_failed"
        if isinstance(exc, DownloadError):
            return "download_failed_all_candidates"
        return "pipeline_error"

    def _ref(self, paper: PaperRecord) -> str:
        return paper.input_value or paper.paper_key

    def _log(self, index: int, total: int, ref: str, message: str, *, level: str = "info") -> None:
        text = f"{index}/{total} | {ref} | {message}"
        if level == "error":
            self.logger.error(text)
        elif level == "warning":
            self.logger.warning(text)
        else:
            self.logger.info(text)