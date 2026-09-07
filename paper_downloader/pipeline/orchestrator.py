from __future__ import annotations

from dataclasses import dataclass, field, fields as dc_fields
from datetime import date
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
from paper_downloader.metadata import record_class as rc
from paper_downloader.metadata import works
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
from paper_downloader.sources.elsevier import ElsevierSourceProvider
from paper_downloader.sources.europepmc import EuropePMCSourceProvider
from paper_downloader.sources.openalex import OpenAlexSourceProvider
from paper_downloader.sources.openalex_content import OpenAlexContentSourceProvider
from paper_downloader.sources.publisher import PublisherLandingSourceProvider
from paper_downloader.sources.wiley import WileySourceProvider
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
    #: FAILURE_REASONS, or None on success and on every `skipped_*` status.
    failure_reason: str | None = None
    #: What kind of record this is, when it is not an ordinary paper: one of
    #: `metadata.record_class.RECORD_CLASSES`, else None. A *terminal* class means there was
    #: never anything to fetch, which is a different fact from a download that failed -- see
    #: `status`, which is then `skipped_<class>`.
    record_class: str | None = None
    #: The signal that decided `record_class`, in a sentence.
    record_class_reason: str | None = None
    #: The article has been retracted. Never stops a fetch; it is a flag for whoever screens
    #: the review, because a retracted paper still has a PDF and still has to be excluded on
    #: purpose rather than by accident.
    retracted: bool = False

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
            "record_class": self.record_class,
            "record_class_reason": self.record_class_reason,
            "retracted": self.retracted,
        }


#: The vocabulary of `DownloadPipelineResult.failure_reason`, most to least decisive when
#: several attempts say different things (see `_failure_reason`).
#:
#: The order is the whole design. A host that never answered about the paper outranks
#: everything, because nothing was learned; a transient error outranks a permanent-looking
#: one, because a re-run might succeed; a page that needs a browser outranks "not free",
#: because a person could still get it. Read top to bottom, this is also the order of how
#: little the result tells you about the paper itself.
FAILURE_REASONS = (
    # A bot wall stopped a non-browser client. Says nothing about the paper, the address or
    # the rate -- and no address or delay changes it. The route is a publisher API, a cached
    # copy elsewhere, or a person with a browser.
    "client_challenged",
    # An edge denial shaped like a rate or IP limit: 429, Retry-After, a refusal that clears
    # from a different network. Worth another attempt later.
    "host_refused_client",
    # A host on the configured deny list was never asked at all.
    "host_denied",
    # 5xx, network error, metadata lookup failed.
    "transient",
    # Reached a real article page with no followable PDF link: a JavaScript download button,
    # a repository landing page with nothing deposited.
    "page_without_link",
    # The arXiv copy was withdrawn by its authors or by arXiv.
    "withdrawn",
    # The record is days or weeks old, the publisher serves HTML and no PDF to anyone yet.
    # The only reason here that time alone fixes.
    "not_yet_available",
    # Every source says closed, or 401/403 on the article itself with no free copy anywhere.
    "not_free",
    # Every URL on record answers 404 and the record is not withdrawn.
    "dead_link",
)

#: How recently published an article has to be for "the PDF is not up yet" to be a more
#: likely explanation than "the publisher will not give it to us". Sixty days is generous;
#: the cases seen were within three weeks of publication.
_NOT_YET_AVAILABLE_DAYS = 60


@dataclass(slots=True)
class _Hop:
    """Where one download attempt actually went, as opposed to where it was aimed.

    A candidate URL is only the start. The attempt may follow a link off a landing page, or
    be rewritten to Europe PMC, and then redirect somewhere else again. Recording only the
    candidate URL -- which is what the manifest used to hold -- makes a failure that followed
    two hops indistinguishable from one that never left the first.
    """
    candidate_url: str
    #: The URL of the request that actually decided the attempt.
    attempted_url: str = ""
    #: The page whose link we followed to get to `attempted_url`, when there was one.
    followed_from: str | None = None
    #: Where that request ended after redirects.
    final_url: str | None = None

    def __post_init__(self) -> None:
        if not self.attempted_url:
            self.attempted_url = self.candidate_url

    def trail(self) -> dict[str, Any]:
        """The fields to merge into a manifest attempt record. Omits what adds nothing."""
        trail: dict[str, Any] = {"candidate_url": self.candidate_url}
        if self.followed_from:
            trail["followed_from"] = self.followed_from
        if self.final_url and self.final_url != self.attempted_url:
            trail["final_url"] = self.final_url
        return trail


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
        self.downloader = downloader or PDFDownloader(config.download, api_config=config.apis)
        self.logger = logging.getLogger("paper_downloader")
        # One session for the classify stage's two metadata lookups. Its answers go into the
        # shared cache, so the crossref, openalex and publisher_landing providers reuse them
        # instead of asking again -- which is what makes classification cost nothing.
        self._metadata_session = host_gate.GatedSession()
        self._metadata_session.headers.update({
            "Accept": "application/json",
            "User-Agent": (
                f"{config.download.user_agent} (mailto:{config.apis.crossref_email})"
                if config.apis.crossref_email else config.download.user_agent
            ),
        })

        # Both are process-wide facts, so they live in host_gate rather than on this
        # object: every downloader and every provider session in the process must agree
        # on which hosts are off limits and which have refused us lately.
        host_gate.set_denied_hosts(config.download.denied_hosts)
        host_gate.configure_persistence(Path(config.output.root_dir) / "host_refusals.json")

        if resolver is None:
            resolver = self._build_resolver(config)
        self.resolver = resolver

    def close(self) -> None:
        """Release the HTTP sessions this orchestrator owns. Safe to call more than once."""
        closer = getattr(self.downloader, "close", None)
        if callable(closer):
            closer()
        if self._metadata_session is not None:
            self._metadata_session.close()

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
            "wiley": lambda: WileySourceProvider(config.apis, config.download, config.resolution),
            "elsevier": lambda: ElsevierSourceProvider(config.apis, config.download, config.resolution),
            "openalex_content": lambda: OpenAlexContentSourceProvider(
                config.apis, config.download, config.resolution
            ),
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

            self._handle_classify_stage(manifest, working_paper, index=index, total=total)
            if working_paper.record_class in rc.TERMINAL_CLASSES:
                return self._skipped_result(manifest, paper, working_paper, index=index, total=total)

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
                record_class=working_paper.record_class,
                record_class_reason=working_paper.record_class_reason,
                retracted=working_paper.retracted,
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
            failure_reason = self._failure_reason(
                failure_code, download_attempts, provider_attempts, paper=working_paper,
            )
            if failure_reason == "withdrawn" and not working_paper.record_class:
                # arXiv answers a withdrawn paper's PDF URL with a plain 404, which reads as
                # a broken link. Ask arXiv once, now that we know it is worth asking.
                self._note_arxiv_withdrawal(working_paper, manifest)
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
                record_class=working_paper.record_class,
                record_class_reason=working_paper.record_class_reason,
                retracted=working_paper.retracted,
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
    def _recently_published(paper: PaperRecord | None) -> bool:
        """Was this article put online recently enough that its PDF may simply not be up yet?"""
        raw = getattr(paper, "published_online", None)
        if not raw:
            return False
        try:
            published = date.fromisoformat(str(raw)[:10])
        except ValueError:
            return False
        return (date.today() - published).days <= _NOT_YET_AVAILABLE_DAYS

    @classmethod
    def _failure_reason(
        cls,
        failure_code: str,
        attempts: list[dict[str, Any]],
        provider_attempts: list[dict[str, Any]],
        *,
        paper: PaperRecord | None = None,
    ) -> str:
        """One word for why there is no PDF, read off what actually happened.

        Several attempts may disagree; the order in FAILURE_REASONS decides -- see the note
        there for why that order is what it is.
        """
        if failure_code == "unresolved_no_legal_pdf":
            return "not_free"
        if failure_code in ("metadata_fetch_failed", "resolution_failed", "pipeline_error"):
            return "transient"
        if not attempts:
            return "transient"

        recent = cls._recently_published(paper)
        found: set[str] = set()
        for a in attempts:
            status = a.get("http_status")
            if a.get("host_blocked"):
                if a.get("host_denied"):
                    found.add("host_denied")
                elif a.get("host_challenged"):
                    found.add("client_challenged")
                else:
                    found.add("host_refused_client")
            elif status is not None and (status >= 500 or status == 429):
                found.add("transient")
            elif a.get("not_a_pdf"):
                # A .pdf URL that answered with the article page, on an article published
                # days ago, is a publisher that has not posted the file yet -- which time
                # fixes on its own. The same thing on a year-old article is not.
                if recent and ".pdf" in str(a.get("pdf_url") or "").lower():
                    found.add("not_yet_available")
                else:
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

    def _note_arxiv_withdrawal(self, paper: PaperRecord, manifest: PipelineManifest | None) -> None:
        """Ask arXiv why an id 404'd, and record `withdrawn` when it says so.

        Costs one API request, and only for a paper whose arXiv PDF has already answered 404.
        Doing it up front would have cost one request per paper with an arXiv id, on a host
        that asks for a three-second gap -- for a fact that matters to roughly one paper in a
        thousand.
        """
        provider = self._provider_named("arxiv")
        note = None
        if provider is not None and paper.arxiv_id:
            note = provider.withdrawal_note(paper.arxiv_id)
        paper.record_class = rc.WITHDRAWN
        paper.record_class_reason = (
            f"arxiv: {note}" if note
            else "arxiv answered 404 for a known id, which it does for withdrawn papers"
        )
        if manifest is not None:
            manifest.paper_snapshot = paper.to_dict()
            self.manifest_store.save(manifest)

    def _provider_named(self, name: str):
        """The resolver's provider with this name, or None if it is not in the chain."""
        for provider in getattr(self.resolver, "providers", []):
            if getattr(provider, "name", None) == name:
                return provider
        return None

    def _handle_classify_stage(
        self,
        manifest: PipelineManifest,
        paper: PaperRecord,
        *,
        index: int,
        total: int,
    ) -> None:
        """Decide what kind of record this is, before spending anything on fetching it.

        Two metadata lookups, both cached and both reused by the providers that come next, so
        the stage is close to free. It answers a question a download failure never can: an
        abstract, a poster and a paywalled article all end up with no PDF, and only this can
        say that for three of them there was never a PDF to get.

        Resumes from the manifest rather than asking again, because the answer is a property
        of the record and records do not change between runs.
        """
        if self._is_stage_completed(manifest, PipelineStage.CLASSIFY_RECORD):
            stored = manifest.get_stage_state(PipelineStage.CLASSIFY_RECORD).details or {}
            paper.record_class = stored.get("record_class")
            paper.record_class_reason = stored.get("reason") or None
            paper.retracted = bool(stored.get("retracted"))
            paper.published_online = stored.get("published_online")
            return

        if not paper.doi:
            self.manifest_store.update_stage(
                manifest, PipelineStage.CLASSIFY_RECORD, StageStatus.SKIPPED,
                message="no doi to classify by",
            )
            return

        timeout = (
            self.config.download.connect_timeout_seconds,
            self.config.download.read_timeout_seconds,
        )
        crossref = works.crossref_work(
            paper.doi, session=self._metadata_session, timeout=timeout,
            verify=self.config.download.verify_ssl,
        ).work
        openalex = works.openalex_work(
            paper.doi, session=self._metadata_session, timeout=timeout,
            verify=self.config.download.verify_ssl,
            api_key=self.config.apis.openalex_api_key,
        ).work

        verdict = rc.classify(crossref=crossref, openalex=openalex, title=paper.title)
        paper.record_class = verdict.record_class
        paper.record_class_reason = verdict.reason or None
        paper.retracted = verdict.retracted
        paper.published_online = verdict.published_online

        self.manifest_store.update_paper_snapshot(manifest, paper)
        self.manifest_store.update_stage(
            manifest, PipelineStage.CLASSIFY_RECORD, StageStatus.SUCCEEDED,
            message=f"record class: {verdict.record_class or 'article'}",
            details={
                "record_class": verdict.record_class,
                "reason": verdict.reason,
                "retracted": verdict.retracted,
                "published_online": verdict.published_online,
            },
        )
        if verdict.record_class:
            self._log(
                index, total, self._ref(paper),
                f"record class {verdict.record_class} ({verdict.reason})"
                + ("; nothing to fetch" if verdict.terminal else ""),
            )
        if verdict.retracted:
            self._log(
                index, total, self._ref(paper),
                "RETRACTED -- fetching anyway; flag this to whoever screens the review",
                level="warning",
            )

    def _skipped_result(
        self,
        manifest: PipelineManifest,
        paper: PaperRecord,
        working_paper: PaperRecord,
        *,
        index: int,
        total: int,
    ) -> DownloadPipelineResult:
        """The result for a record with nothing to fetch. No provider was asked, on purpose.

        `failure_reason` is None and the status is `skipped_<class>`, so a caller can keep
        these out of "could not retrieve" -- and out of a "retry the failures" run, where
        they would burn the whole provider chain forever for a paper that does not exist as
        a document.
        """
        self.manifest_store.update_stage(
            manifest, PipelineStage.RESOLVE_SOURCE, StageStatus.SKIPPED,
            message=f"{working_paper.record_class}: nothing to fetch",
        )
        self.manifest_store.update_stage(
            manifest, PipelineStage.DOWNLOAD_PDF, StageStatus.SKIPPED,
            message=f"{working_paper.record_class}: nothing to fetch",
        )
        manifest.batch_status = BatchStatus.PARTIAL_SUCCESS
        manifest.stats["record_class"] = working_paper.record_class
        self.manifest_store.save(manifest)
        self._log(
            index, total, self._ref(paper),
            f"skipped: {working_paper.record_class} -- {working_paper.record_class_reason}",
        )
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
            status=f"skipped_{working_paper.record_class}",
            manifest_path=str(self.manifest_store.path_for(working_paper.paper_key)),
            failure_reason=None,
            record_class=working_paper.record_class,
            record_class_reason=working_paper.record_class_reason,
            retracted=working_paper.retracted,
        )

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
                hop = _Hop(candidate_url=candidate.pdf_url)
                try:
                    try:
                        result = self.downloader.download(
                            candidate.pdf_url,
                            output_pdf_path,
                            skip_if_valid=self.config.resume.verify_existing_files,
                        )
                    except NotAPDFError as exc:
                        hop.final_url = exc.final_url or None
                        result = self._retry_via_landing_page(
                            exc, candidate, output_pdf_path, hop,
                            index=index, total=total, paper=paper,
                        )
                    hop.final_url = result.final_url or hop.final_url
                    attempts.append({
                        "candidate_index": attempt_no,
                        "round": round_no,
                        "source_name": candidate.source_name,
                        "pdf_url": hop.attempted_url,
                        "status": "succeeded",
                        "error": None,
                        "result": result.to_dict(),
                        **hop.trail(),
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
                    if isinstance(exc, NotAPDFError) and exc.final_url:
                        hop.final_url = exc.final_url
                    attempts.append({
                        "candidate_index": attempt_no,
                        "round": round_no,
                        "source_name": candidate.source_name,
                        "pdf_url": hop.attempted_url,
                        "status": "failed",
                        "error": str(exc),
                        "http_status": getattr(exc, "status_code", None),
                        "not_a_pdf": isinstance(exc, NotAPDFError),
                        # Whether the host turned this server away rather than answering
                        # about the paper. A caller deciding "is this paper worth another
                        # attempt" cannot tell from the status alone -- see host_gate.
                        "host_blocked": isinstance(exc, HostBlockedError),
                        # ...and which of the three kinds it was. All three read as "no PDF"
                        # and all three have different remedies, so a caller that flattens
                        # them retries the hopeless and gives up on the recoverable.
                        "host_denied": bool(getattr(exc, "denied", False)),
                        "host_challenged": bool(getattr(exc, "challenge", False)),
                        "host": getattr(exc, "host", "") or host_gate.host_key(hop.attempted_url),
                        **hop.trail(),
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
        hop: "_Hop",
        *,
        index: int,
        total: int,
        paper: PaperRecord,
    ) -> DownloadResult:
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

        Every hop it takes is written into `hop`, so a failed follow-up is legible afterwards.
        Without that the manifest recorded only the landing URL, and a Nature article page
        that had actually been followed to a `.pdf` link and bounced straight back looked
        identical to one that offered no link at all -- four extra requests to find out.
        """
        if not self.config.download.landing_page_fallback:
            raise exc

        rewritten = self._pmc_rewrite(exc)
        if rewritten:
            self._log(
                index, total, self._ref(paper),
                f"{candidate.source_name} hit PMC's download interstitial | fetching from Europe PMC | {rewritten}",
            )
            hop.attempted_url = rewritten
            hop.followed_from = exc.final_url or candidate.pdf_url
            return self.downloader.download(
                rewritten, output_pdf_path, skip_if_valid=self.config.resume.verify_existing_files,
            )

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
        hop.attempted_url = pdf_url
        hop.followed_from = landing_url
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
            hop.final_url = second.final_url or hop.final_url
            rewritten = self._pmc_rewrite(second)
            if not rewritten:
                raise
            self._log(
                index, total, self._ref(paper),
                f"{candidate.source_name}'s PDF link is PMC's download interstitial | fetching from Europe PMC | {rewritten}",
            )
            hop.followed_from = hop.attempted_url
            hop.attempted_url = rewritten
            return self.downloader.download(
                rewritten, output_pdf_path, skip_if_valid=self.config.resume.verify_existing_files,
            )
        return result

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