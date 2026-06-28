from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class SemanticScholarConfig:
    fields: str
    bulk_search_url: str = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"


@dataclass
class SearchQuery:
    """One atomic Semantic Scholar bulk-search request, ready to execute."""
    id: str
    query: str
    # SS API param names (e.g. "publicationDateOrYear") → values, already translated.
    ss_params: dict


@dataclass
class RecoveryConfig:
    similarity_threshold: float
    min_abstract_len: int
    request_delay: float
    scrape_timeout: tuple[int, int]
    scrape_max_retries: int
    api_sleep_between_papers: float


@dataclass
class OutputConfig:
    base_dir: str


@dataclass
class CitationGraphConfig:
    """
    Static defaults for the citations/references retrieval feature, loaded from
    the 'citation_graph' section of config.json.  Every field can be overridden
    at call time via CitationGraphOptions.
    """
    fields: str
    # None means "fetch all edges up to the API ceiling (~9,999 per endpoint)".
    max_results: int | None
    # Seconds to sleep between papers in a batch call (not between pages).
    request_delay: float
    base_url: str = "https://api.semanticscholar.org/graph/v1/paper"


@dataclass
class CitationGraphOptions:
    """
    Per-call, per-endpoint overrides.  Any field left as None falls back to the
    value in CitationGraphConfig.  Construct one instance for both endpoints
    (common case) or two separate instances for fine-grained control.
    """
    fields: str | None = None
    max_results: int | None = None
    influential_only: bool = False
    # Honoured only by the citations endpoint; silently ignored for references.
    publication_date_filter: str | None = None


@dataclass
class PaperGraphResult:
    """
    Returned by fetch_citations_and_references() for each input identifier.
    Lists are empty (not None) when the corresponding endpoint was not requested
    or when the paper lookup failed.
    """
    input_id: str
    # SS canonical paperId resolved by the API; None if the lookup failed.
    paper_id: str | None
    citations: list[dict]
    references: list[dict]
    citations_fetched: int       # raw count before influential_only filtering
    references_fetched: int
    # True when the API ceiling (~9,999) was hit and results were truncated.
    citations_truncated: bool
    references_truncated: bool
    error: str | None            


@dataclass
class MetadataConfig:
    semantic_scholar: SemanticScholarConfig
    recovery: RecoveryConfig
    output: OutputConfig
    citation_graph: CitationGraphConfig
    search_queries_path: str
    ss_api_key: str = ""
    core_api_key: str = ""


@dataclass
class RunConfig:
    run_api_recovery: bool = False
    run_scrape_recovery: bool = False
    input_dir: Path | None = None
    source_label: str | None = None
    label: str | None = None


@dataclass
class RunSummary:
    """Parsed view of a run.json manifest, returned by list_runs()."""
    run_id: str
    label: str | None
    started_at: str
    completed_at: str | None
    status: str
    run_dir: Path
    counts: dict