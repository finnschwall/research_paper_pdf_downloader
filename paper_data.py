from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).parent.resolve()
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from paper_downloader.config.loader import load_config as _dl_load_config
from paper_downloader.config.models import PipelineConfig
from paper_downloader.config.validator import validate_config
from paper_downloader.pipeline.orchestrator import DownloadOrchestrator, DownloadPipelineResult

from paper_metadata.acquisition.citations import fetch_paper_graph
from paper_metadata.config.loader import load_config as _md_load_config
from paper_metadata.config.models import (
    CitationGraphOptions,
    MetadataConfig,
    PaperGraphResult,
    RunConfig,
    RunSummary,
)
from paper_metadata.pipeline.orchestrator import MetadataOrchestrator
from paper_metadata.acquisition.semantic_scholar import fetch_papers_by_ids as _fetch_papers_by_ids
from paper_metadata.recovery.api_recovery import ApiRecoveryProvider
from paper_metadata.recovery.scrape_recovery import ScrapeRecoveryProvider

logging.getLogger("paper_downloader").addHandler(logging.NullHandler())
logging.getLogger("paper_metadata").addHandler(logging.NullHandler())

_DEFAULT_DOWNLOAD_CONFIG = Path(__file__).parent / "config.json"
_DEFAULT_METADATA_CONFIG = Path(__file__).parent / "paper_metadata" / "config.json"


# ── paper_downloader ──────────────────────────────────────────────────────────

def download(
    ids: str | list[str] | Path,
    *,
    config: PipelineConfig | None = None,
    config_path: str | Path | None = None,
) -> list[DownloadPipelineResult]:
    """
    Download open-access PDFs for one or more paper identifiers.

    Accepts a single identifier string (Semantic Scholar ID, DOI, ArXiv ID, etc.),
    a list of identifier strings, or a path to a JSON file containing a list of
    identifiers or full metadata records. Returns one DownloadPipelineResult
    per input paper; check result.downloaded and result.pdf_path on each.
    """
    resolved_ids = _resolve_ids(ids)
    resolved_config = _resolve_download_config(config, config_path)
    orchestrator = DownloadOrchestrator(resolved_config)
    try:
        return orchestrator.process_inputs(resolved_ids)
    finally:
        # The downloader keeps one HTTP session open across a call now, so a one-shot
        # `download()` has something to release. `open_downloader` is the door for callers
        # walking a list; this one builds and discards an orchestrator per call.
        orchestrator.close()


class DownloaderHandle:
    """A downloader kept open across many papers.

    ``download()`` builds a fresh orchestrator per call: eight provider HTTP sessions, a
    Semantic Scholar client, and a re-read of config.json. That is wasted work when a
    caller is walking a list of papers one at a time -- which it must, if it wants to
    record progress after each one rather than only at the end of a batch.

    Not thread-safe, on purpose: the providers hold ``requests.Session`` objects, which are
    not safe to share. A caller wanting parallelism should open one handle per thread,
    which costs nothing beyond the sessions each thread was going to need anyway.
    """

    def __init__(self, config: PipelineConfig) -> None:
        self._orchestrator = DownloadOrchestrator(config)

    def download_one(self, identifier: str) -> DownloadPipelineResult:
        """Download one paper. Returns a result even on failure; never raises for a
        per-paper problem -- check ``.downloaded`` and ``.failure_code``."""
        results = self._orchestrator.process_inputs([identifier])
        return results[0]

    def __enter__(self) -> "DownloaderHandle":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        """Release the HTTP session held open across downloads."""
        self._orchestrator.close()


def open_downloader(
    *,
    config: PipelineConfig | None = None,
    config_path: str | Path | None = None,
) -> DownloaderHandle:
    """Open a reusable downloader; see DownloaderHandle.

        with paper_data.open_downloader(config=cfg) as dl:
            for paper_id in ids:
                result = dl.download_one(paper_id)
    """
    return DownloaderHandle(_resolve_download_config(config, config_path))


def _resolve_ids(ids: str | list[str] | Path) -> list[str]:
    """
    Normalise the ids argument into a plain list of identifier strings.

    If ids is already a list it is returned as-is. If it is a path to an
    existing .json file, the file is read and its contents returned as a
    list. Any other string is treated as a single identifier and wrapped in a
    one-element list.
    """
    if isinstance(ids, list):
        return ids

    path = Path(ids)

    if path.suffix.lower() == ".json" and path.exists():
        return _load_ids_from_json(path)

    return [str(ids)]


def _load_ids_from_json(path: Path) -> list[str]:
    """
    Read a JSON file and return its contents as a list of identifier strings.

    The file must contain a JSON array of strings. Blank entries are silently
    dropped. Raises ValueError if the file is not valid JSON or does not
    contain a non-empty array.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in file {path}: {exc}") from exc

    if not isinstance(data, list):
        raise ValueError(
            f"JSON file must contain a list of paper ID strings. "
            f"Got {type(data).__name__} in: {path}"
        )

    ids = [str(item).strip() for item in data if str(item).strip()]

    if not ids:
        raise ValueError(f"No paper IDs found in JSON file: {path}")

    return ids


def _resolve_download_config(
    config: PipelineConfig | None,
    config_path: str | Path | None,
) -> PipelineConfig:
    """
    Return the PipelineConfig to use for a download run.

    If a pre-built config object is supplied it is used directly. Otherwise
    the config is loaded and validated from config_path, falling back to the
    default config.json that ships alongside this file.
    """
    if config is not None:
        return config
    path = Path(config_path) if config_path is not None else _DEFAULT_DOWNLOAD_CONFIG
    loaded = _dl_load_config(path)
    validate_config(loaded)
    return loaded


# ── paper_metadata — bulk pipeline ────────────────────────────────────────────

def _is_missing_abstract(paper: dict) -> bool:
    abstract = paper.get("abstract")
    return not abstract or not str(abstract).strip()


def fetch_metadata(
    *,
    config: MetadataConfig | None = None,
    config_path: str | Path | None = None,
    base_dir: str | Path | None = None,
    search_queries_path: str | Path | None = None,
    label: str | None = None,
    api_recovery: bool = True,
    scrape_recovery: bool = True,
) -> None:
    resolved_config = _resolve_metadata_config(config, config_path, base_dir, search_queries_path)
    run_config = RunConfig(
        run_api_recovery=api_recovery,
        run_scrape_recovery=scrape_recovery,
        label=label,
    )
    MetadataOrchestrator(resolved_config, run_config).run()


def preview_queries(
    *,
    config: MetadataConfig | None = None,
    config_path: str | Path | None = None,
    search_queries_path: str | Path | None = None,
) -> dict[str, int]:
    """
    Return estimated result counts for each query in search_queries.json without
    fetching any paper data. One lightweight API request per query.

    Returns {query_id: estimated_count}.
    """
    from paper_metadata.acquisition.semantic_scholar import load_search_queries, preview_all_queries

    resolved_config = _resolve_metadata_config(config, config_path, None, search_queries_path)
    search_queries = load_search_queries(resolved_config.search_queries_path)
    return preview_all_queries(search_queries, resolved_config)


def sample_queries(
    *,
    config: MetadataConfig | None = None,
    config_path: str | Path | None = None,
    search_queries_path: str | Path | None = None,
    n: int = 20,
) -> dict[str, list[dict]]:
    """
    Fetch the first n papers for each query without full pagination.
    One API request per query — fast sanity check before a full run.

    Returns {query_id: [paper_dict, ...]} where each dict has:
    paperId, title, abstract, year, citationCount, authors, venue.
    """
    from paper_metadata.acquisition.semantic_scholar import load_search_queries, sample_all_queries

    resolved_config = _resolve_metadata_config(config, config_path, None, search_queries_path)
    search_queries = load_search_queries(resolved_config.search_queries_path)
    return sample_all_queries(search_queries, resolved_config, n=n)


def list_runs(base_dir: str | Path) -> list[RunSummary]:
    """
    Scan <base_dir>/runs/ for completed and in-progress runs, returning them
    sorted newest-first. Each entry is a RunSummary parsed from run.json.
    """
    runs_dir = Path(base_dir).resolve() / "runs"
    if not runs_dir.exists():
        return []

    summaries: list[RunSummary] = []
    for run_json_path in sorted(runs_dir.glob("*/run.json"), reverse=True):
        try:
            data = json.loads(run_json_path.read_text(encoding="utf-8"))
            summaries.append(RunSummary(
                run_id=data["run_id"],
                label=data.get("label"),
                started_at=data.get("started_at", ""),
                completed_at=data.get("completed_at"),
                status=data.get("status", "unknown"),
                run_dir=run_json_path.parent,
                counts=data.get("counts", {}),
            ))
        except Exception:
            pass

    return summaries


def recover_abstracts(
    input_dir: str | Path,
    *,
    config: MetadataConfig | None = None,
    config_path: str | Path | None = None,
    base_dir: str | Path | None = None,
    api_recovery: bool = True,
    scrape_recovery: bool = True,
) -> None:
    input_path = Path(input_dir).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"input_dir does not exist: {input_path}")

    resolved_config = _resolve_metadata_config(config, config_path, base_dir, None)
    run_config = RunConfig(
        run_api_recovery=api_recovery,
        run_scrape_recovery=scrape_recovery,
        input_dir=input_path,
    )
    MetadataOrchestrator(resolved_config, run_config).run()


def fetch_papers_by_id(
    ids: str | list[str],
    *,
    config: MetadataConfig | None = None,
    config_path: str | Path | None = None,
    api_recovery: bool = False,
    scrape_recovery: bool = False,
) -> list[dict]:
    """
    Fetch full metadata for one or more paper identifiers directly from
    Semantic Scholar, without running the keyword search pipeline.

    Accepts a single identifier string or a list. Each identifier can be a
    Semantic Scholar paper ID, ArXiv ID, or any recognised by SS
    (ARXIV:, DOI:, ACL:, MAG:, PMID:, PMCID:, CorpusId:).

    Returns a list parallel to the input. Each dict contains the full paper
    metadata in the same schema used throughout the pipeline, plus two
    diagnostic fields:
        _input_id     – the original identifier supplied by the caller
        _fetch_status – 'found' | 'not_found' | 'invalid_id'

    When api_recovery or scrape_recovery is True, papers found in SS but
    missing an abstract are passed through the respective recovery providers
    in-memory. No disk I/O or pipeline orchestration is involved.
    """
    resolved_ids    = [ids] if isinstance(ids, str) else list(ids)
    resolved_config = _resolve_metadata_config(config, config_path, None, None)
    results         = _fetch_papers_by_ids(resolved_ids, resolved_config)

    if not (api_recovery or scrape_recovery):
        return results

    found_papers   = [r for r in results if r.get("_fetch_status") == "found"]
    missing_papers = [p for p in found_papers if _is_missing_abstract(p)]

    if not missing_papers:
        return results

    if api_recovery:
        provider = ApiRecoveryProvider(
            config       = resolved_config.recovery,
            ss_api_key   = resolved_config.ss_api_key,
            core_api_key = resolved_config.core_api_key,
        )
        for paper in missing_papers:
            result = provider.recover(paper)
            if result:
                paper["abstract"] = result.abstract

    if scrape_recovery:
        still_missing = [p for p in missing_papers if _is_missing_abstract(p)]
        provider = ScrapeRecoveryProvider(config=resolved_config.recovery)
        for paper in still_missing:
            result = provider.recover(paper)
            if result:
                paper["abstract"] = result.abstract

    return results


# ── paper_metadata — citation / reference graph ───────────────────────────────

def fetch_citations_and_references(
    ids: str | list[str],
    *,
    # Which endpoints to call
    citations: bool = True,
    references: bool = True,
    # Shorthand kwargs — applied identically to both endpoints when no
    # explicit citation_options / reference_options are supplied.
    influential_only: bool = False,
    fields: str | None = None,
    max_results: int | None = None,
    publication_date_filter: str | None = None,
    # Fine-grained per-endpoint control. When provided, ALL shorthand kwargs
    # are ignored for that endpoint — the options object is used as-is.
    citation_options: CitationGraphOptions | None = None,
    reference_options: CitationGraphOptions | None = None,
    # Output — optional JSON persistence
    save_dir: str | Path | None = None,
    # Config
    config: MetadataConfig | None = None,
    config_path: str | Path | None = None,
) -> list[PaperGraphResult]:
    """
    Fetch citation and/or reference edges for one or more papers.

    Parameters
    ----------
    ids
        A single identifier string or a list of them. Each identifier is
        auto-detected and normalised — no manual prefix required for the
        common formats:

          40-char hex string        → Semantic Scholar SHA paper ID
          10.XXXX/...               → DOI  (prefixed automatically as DOI:...)
          2106.15928 / 2106.15928v2
          cs/0612033                → ArXiv  (prefixed as ARXIV:...)
          https://arxiv.org/...     → URL  (prefixed as URL:...)

        Explicit prefixes are also accepted and are case-insensitive on the
        prefix part:  "doi:...", "ARXIV:...", "CorpusId:...", "PMID:...",
        "PMCID:...", "MAG:...", "ACL:...".

        Bare numeric strings (e.g. "12345678") are rejected with a ValueError
        because they are ambiguous across CorpusId, PubMed, and MAG namespaces.
        Use an explicit prefix instead: "CorpusId:12345678".

    citations
        Whether to call the /citations endpoint.  Default True.

    references
        Whether to call the /references endpoint.  Default True.

    influential_only
        Shorthand: when True, both citation and reference lists are filtered
        to edges where isInfluential is True. Filtering is applied in Python
        after fetching; all edges are still fetched from the API first.
        Ignored if citation_options / reference_options are supplied explicitly
        for the respective endpoint.

    fields
        Shorthand: comma-separated list of paper fields to return for each
        cited/citing paper (e.g. "paperId,title,year,abstract,authors").
        Falls back to the config default when None.
        Ignored if citation_options / reference_options are supplied.

    max_results
        Shorthand: cap on the number of edges fetched per endpoint per paper.
        None means fetch all edges up to the API ceiling (~9,999).
        Falls back to the config default when None.
        Ignored if citation_options / reference_options are supplied.

    publication_date_filter
        Shorthand: date-range string applied to the citations endpoint only
        (the references endpoint does not support this parameter).
        Format: "YYYY-MM-DD:YYYY-MM-DD", open-ended ("2020-01-01:" / ":2023-12-31").
        Ignored if citation_options is supplied explicitly.

    citation_options
        Fine-grained control for the citations endpoint only. When supplied,
        ALL shorthand kwargs (influential_only, fields, max_results,
        publication_date_filter) are ignored for citations. Providing both
        citation_options and any shorthand kwarg at a non-default value raises
        ValueError.

    reference_options
        Fine-grained control for the references endpoint only. When supplied,
        ALL shorthand kwargs except publication_date_filter (which is
        citations-only) are ignored for references. Providing both
        reference_options and any shorthand kwarg at a non-default value raises
        ValueError.

    save_dir
        Optional path under which citation and reference JSON files are
        written.  The layout created is:

          save_dir/
            citations/<paper_id>.json
            references/<paper_id>.json

        When None (the default), no files are written.  To write inside
        the pipeline's existing output tree, pass the search_results path:

          save_dir = Path(config.output.base_dir) / "search_results"

    config
        Pre-built MetadataConfig. When provided, config_path is ignored.

    config_path
        Path to a paper_metadata config.json. Falls back to the default
        config.json that ships with the library.

    Returns
    -------
    list[PaperGraphResult]
        One result per input identifier, in input order. Check result.error
        for identifiers that could not be resolved. result.citations and
        result.references are always lists (empty when not requested or on
        error). result.citations_fetched / references_fetched report the raw
        edge count before any influential_only filtering.
        result.citations_truncated / references_truncated are True when the
        API ceiling was hit.

    Examples
    --------
    # Single paper, both endpoints, all defaults
    results = fetch_citations_and_references("2106.15928")

    # Batch, influential citations only, references off
    results = fetch_citations_and_references(
        ["DOI:10.18653/v1/N18-3011", "2106.15928"],
        references=False,
        influential_only=True,
    )

    # Different options per endpoint
    from paper_data import CitationGraphOptions
    results = fetch_citations_and_references(
        "CorpusId:215416146",
        citation_options=CitationGraphOptions(influential_only=True, max_results=200),
        reference_options=CitationGraphOptions(fields="paperId,title,year"),
    )
    """
    if not citations and not references:
        raise ValueError("At least one of citations or references must be True.")

    _validate_shorthand_conflicts(
        citation_options, reference_options,
        influential_only, fields, max_results, publication_date_filter,
    )

    resolved_config = _resolve_metadata_config(config, config_path, None, None)

    effective_citation_opts = citation_options or CitationGraphOptions(
        fields=fields,
        max_results=max_results,
        influential_only=influential_only,
        publication_date_filter=publication_date_filter,
    )
    effective_reference_opts = reference_options or CitationGraphOptions(
        fields=fields,
        max_results=max_results,
        influential_only=influential_only,
        # publication_date_filter is citations-only; omitted here explicitly.
        publication_date_filter=None,
    )

    resolved_save_dir: Path | None = None
    if save_dir is not None:
        resolved_save_dir = Path(save_dir).resolve()

    return fetch_paper_graph(
        ids,
        cfg=resolved_config.citation_graph,
        ss_api_key=resolved_config.ss_api_key,
        fetch_citations=citations,
        fetch_references=references,
        citation_options=effective_citation_opts,
        reference_options=effective_reference_opts,
        save_dir=resolved_save_dir,
    )


# ── SEER ingest bundle exports ────────────────────────────────────────────────

def export_keyword_bundle(
    *,
    bundle_dir: str | Path | None = None,
    source_label: str | None = None,
    label: str | None = None,
    config: MetadataConfig | None = None,
    config_path: str | Path | None = None,
    base_dir: str | Path | None = None,
    search_queries_path: str | Path | None = None,
    api_recovery: bool = True,
    scrape_recovery: bool = True,
) -> Path:
    """
    Run the full keyword-search pipeline and write a SEER ingest bundle.

    The bundle is written to bundle_dir (default:
    <base_dir>/search_results/seer_ingest/).  All pipeline outputs are also
    written to the usual search_results/ subdirectories.

    Returns the Path of the written bundle directory.
    """
    from paper_metadata.export.seer_bundle import get_library_version
    resolved_config = _resolve_metadata_config(config, config_path, base_dir, search_queries_path)

    if bundle_dir is not None:
        resolved_config.output.base_dir = str(
            Path(resolved_config.output.base_dir).resolve()
        )

    run_config = RunConfig(
        run_api_recovery=api_recovery,
        run_scrape_recovery=scrape_recovery,
        source_label=source_label,
        label=label,
    )
    result_path = MetadataOrchestrator(resolved_config, run_config).run()

    if bundle_dir is not None:
        import shutil
        default_bundle = result_path
        target = Path(bundle_dir).resolve()
        if default_bundle and default_bundle != target:
            target.mkdir(parents=True, exist_ok=True)
            for fname in ("manifest.json", "papers.json"):
                src = default_bundle / fname
                if src.exists():
                    shutil.copy2(src, target / fname)
        return target

    return result_path


def export_by_id_bundle(
    ids: str | list[str],
    *,
    bundle_dir: str | Path,
    source_label: str | None = None,
    config: MetadataConfig | None = None,
    config_path: str | Path | None = None,
    api_recovery: bool = False,
    scrape_recovery: bool = False,
) -> Path:
    """
    Fetch metadata for explicit paper IDs and write a SEER ingest bundle.

    Returns the Path of the written bundle directory.
    """
    from paper_metadata.export.seer_bundle import get_library_version, write_bundle

    papers = fetch_papers_by_id(
        ids,
        config=config,
        config_path=config_path,
        api_recovery=api_recovery,
        scrape_recovery=scrape_recovery,
    )

    for p in papers:
        p["_provenance"] = {
            "matched_queries": [],
            "seed_paper_id": None,
            "edge_type": None,
            "is_influential": None,
            "input_id": p.pop("_input_id", None),
            "fetch_status": p.pop("_fetch_status", None),
        }

    bundle_path = Path(bundle_dir).resolve()
    write_bundle(
        bundle_path,
        run_type="by_id",
        papers=papers,
        manifest_extra={
            "queries": {},
            "seeds": [],
            "fetch_params": {},
            "source_label": source_label,
        },
        library_version=get_library_version(),
    )
    return bundle_path


def export_citation_bundle(
    ids: str | list[str],
    *,
    bundle_dir: str | Path,
    source_label: str | None = None,
    citations: bool = True,
    references: bool = True,
    config: MetadataConfig | None = None,
    config_path: str | Path | None = None,
    **kwargs,
) -> Path:
    """
    Fetch citation/reference graph for seed papers and write a SEER ingest bundle.

    Returns the Path of the written bundle directory.
    """
    from paper_metadata.export.seer_bundle import get_library_version, write_bundle

    graph_results = fetch_citations_and_references(
        ids,
        citations=citations,
        references=references,
        config=config,
        config_path=config_path,
        **kwargs,
    )

    flat_papers: list[dict] = []
    seeds: list[dict] = []

    for result in graph_results:
        edges_fetched = []
        if citations and result.citations:
            edges_fetched.append("citations")
        if references and result.references:
            edges_fetched.append("references")
        seeds.append({"seed_paper_id": result.paper_id, "edges": edges_fetched})

        for p in result.citations:
            p["_provenance"] = {
                "matched_queries": [],
                "seed_paper_id": result.paper_id,
                "edge_type": "citation",
                "is_influential": p.pop("_is_influential", False),
                "input_id": None,
                "fetch_status": None,
            }
            flat_papers.append(p)

        for p in result.references:
            p["_provenance"] = {
                "matched_queries": [],
                "seed_paper_id": result.paper_id,
                "edge_type": "reference",
                "is_influential": p.pop("_is_influential", False),
                "input_id": None,
                "fetch_status": None,
            }
            flat_papers.append(p)

    bundle_path = Path(bundle_dir).resolve()
    write_bundle(
        bundle_path,
        run_type="citation_graph",
        papers=flat_papers,
        manifest_extra={
            "queries": {},
            "seeds": seeds,
            "fetch_params": {},
            "source_label": source_label,
        },
        library_version=get_library_version(),
    )
    return bundle_path


# ── Internal helpers ──────────────────────────────────────────────────────────

def _validate_shorthand_conflicts(
    citation_options: CitationGraphOptions | None,
    reference_options: CitationGraphOptions | None,
    influential_only: bool,
    fields: str | None,
    max_results: int | None,
    publication_date_filter: str | None,
) -> None:
    """
    Raise ValueError if the caller supplies both an explicit options object
    and shorthand kwargs that would silently be ignored.

    A shorthand kwarg is considered non-default when it differs from its
    parameter default: influential_only=False, fields=None, max_results=None,
    publication_date_filter=None.
    """
    shorthand_active = (
        influential_only is not False
        or fields is not None
        or max_results is not None
        or publication_date_filter is not None
    )

    if citation_options is not None and shorthand_active:
        raise ValueError(
            "Provide either citation_options or shorthand kwargs "
            "(influential_only, fields, max_results, publication_date_filter), "
            "not both. The shorthand kwargs are ignored when citation_options "
            "is supplied, which would produce a silently surprising result."
        )

    if reference_options is not None and shorthand_active:
        raise ValueError(
            "Provide either reference_options or shorthand kwargs "
            "(influential_only, fields, max_results), not both. "
            "The shorthand kwargs are ignored when reference_options is "
            "supplied, which would produce a silently surprising result."
        )


def _resolve_metadata_config(
    config: MetadataConfig | None,
    config_path: str | Path | None,
    base_dir: str | Path | None,
    search_queries_path: str | Path | None,
) -> MetadataConfig:
    if config is not None:
        resolved = config
    else:
        path = Path(config_path) if config_path is not None else _DEFAULT_METADATA_CONFIG
        resolved = _md_load_config(path)

    if base_dir is not None:
        resolved.output.base_dir = str(Path(base_dir).resolve())

    if search_queries_path is not None:
        resolved.search_queries_path = str(Path(search_queries_path).resolve())

    return resolved