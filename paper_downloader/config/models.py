from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RuntimeConfig:
    input_path: str | None = None


@dataclass(slots=True)
class ApiConfig:
    """Keys and contact addresses the providers need.

    None or empty switches the matching provider off rather than making it fail: a
    deployment without a Wiley token simply never asks Wiley. See
    `paper_downloader.core.credentials` for how the three secret ones reach a request
    without ever entering a URL.
    """
    semantic_scholar_api_key: str | None = None
    #: Raises OpenAlex's rate limit, and is *required* for the metered full-text cache
    #: (`sources/openalex_content.py`) -- that endpoint answers 401 without one.
    openalex_api_key: str | None = None
    #: Unpaywall returns nothing at all without a contact address.
    unpaywall_email: str | None = None
    core_api_key: str | None = None
    #: Puts Crossref requests in its faster "polite pool".
    crossref_email: str | None = None
    #: Wiley text-and-data-mining token (a UUID, from Wiley's TDM page after the
    #: click-through licence). Authorisation is by institutional IP range as well, so the
    #: same token works from a subscribing network and not from a home connection.
    wiley_tdm_token: str | None = None
    #: Elsevier Article Retrieval API key, self-registered at dev.elsevier.com.
    elsevier_api_key: str | None = None
    #: Optional Elsevier institutional token, which extends the key's entitlement to the
    #: subscribing institution's holdings.
    elsevier_inst_token: str | None = None


@dataclass(slots=True)
class ResolutionConfig:
    prefer_publisher_version: bool = True
    allow_preprints: bool = True
    allow_title_fallback: bool = True
    title_similarity_threshold: float = 0.90

    # Stop querying further providers once one has already produced a candidate good
    # enough that no later provider could outrank it: a direct .pdf link, on a trusted
    # host, scoring at or above stop_confidence_threshold. Without this every paper pays
    # for all twelve providers even when the first one hands back an arXiv PDF URL.
    stop_when_confident: bool = True
    stop_confidence_threshold: float = 0.75

    source_priority: list[str] = field(
        default_factory=lambda: [
            "metadata_open_access",
            "arxiv",
            "acl",
            "cvf",
            "openalex",
            "unpaywall",
            "europepmc",
            "crossref",
            "core",
            "zenodo",
            "doaj",
            # Publisher text-and-data-mining APIs. Later than the free aggregators because
            # a repository copy costs the publisher nothing, earlier than the last resorts
            # because they are sanctioned routes that actually work where the website does
            # not. Each is inert until its key is configured.
            "wiley",
            "elsevier",
            "broad_search",
            # Metered: OpenAlex charges about a cent per cached PDF, so it is asked only
            # once every free route has been tried and failed. See sources/openalex_content.py.
            "openalex_content",
            # Last on purpose: it asks the publisher, which every provider above exists to
            # avoid. See sources/publisher.py.
            "publisher_landing",
        ]
    )

    trusted_domains: list[str] = field(
        default_factory=lambda: [
            "arxiv.org",
            "biorxiv.org",
            "medrxiv.org",
            "aclanthology.org",
            "openaccess.thecvf.com",
            "api.openalex.org",
            "content.openalex.org",
            "api.wiley.com",
            "api.elsevier.com",
            "doi.org",
            "dl.acm.org",
            "ieeexplore.ieee.org",
            "link.springer.com",
            "nature.com",
            "sciencedirect.com",
            "pmc.ncbi.nlm.nih.gov",
            "europepmc.org",
            "zenodo.org",
            "doaj.org",
            "osf.io",
            "hal.science",
            "ssrn.com",
            "core.ac.uk",
            "mdpi.com",
            "frontiersin.org",
            "journals.plos.org",
        ]
    )


@dataclass(slots=True)
class DownloadConfig:
    connect_timeout_seconds: int = 10
    read_timeout_seconds: int = 60
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    verify_ssl: bool = True
    max_redirects: int = 5
    min_pdf_bytes: int = 1024
    max_pdf_bytes: int = 250_000_000
    user_agent: str = "paper-downloader/1.0"

    # When a candidate URL turns out to serve HTML rather than a PDF, parse that page for
    # a link to the actual PDF (citation_pdf_url meta tag, OJS download link, same-host
    # .pdf anchor) and try it once. Most "gold OA" DOIs point at a landing page, not a file.
    landing_page_fallback: bool = True
    landing_page_max_bytes: int = 2_000_000

    # Hosts never to contact, matched by dotted suffix ("acm.org" covers "dl.acm.org").
    # For publishers known to refuse this client outright: a candidate on a denied host is
    # kept but tried last and never ends the provider search, and the download stage fails
    # it as host-blocked without a request -- so the paper is reported as retryable, not
    # as having no open-access copy. Configuration, not a verdict on any paper.
    denied_hosts: list[str] = field(default_factory=list)

    # Read the front pages of every downloaded PDF and check that the paper's own DOI, arXiv
    # id or title is on them. A file that is legibly a different document is discarded and
    # the next candidate tried; a file that cannot be checked is kept and marked. Costs a
    # pypdf parse per download, a quarter of a second on the median paper. See
    # paper_downloader.download.identity.
    verify_identity: bool = True

    allowed_content_types: list[str] = field(
        default_factory=lambda: [
            "application/pdf",
            "application/x-pdf",
            "binary/octet-stream",
        ]
    )


@dataclass(slots=True)
class ResumeConfig:
    enabled: bool = True
    skip_completed_stages: bool = True
    verify_existing_files: bool = True
    retry_failed_stage_only: bool = True


@dataclass(slots=True)
class OutputConfig:
    root_dir: str = "data"
    input_dir_name: str = "input"
    metadata_dir_name: str = "metadata"
    manifests_dir_name: str = "manifests"
    pdfs_dir_name: str = "pdfs"
    reports_dir_name: str = "reports"


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    log_file: str | None = None


@dataclass(slots=True)
class PipelineConfig:
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    apis: ApiConfig = field(default_factory=ApiConfig)
    resolution: ResolutionConfig = field(default_factory=ResolutionConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    resume: ResumeConfig = field(default_factory=ResumeConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PipelineConfig":
        return cls(
            runtime=RuntimeConfig(**dict(data.get("runtime") or {})),
            apis=ApiConfig(**dict(data.get("apis") or {})),
            resolution=ResolutionConfig(**dict(data.get("resolution") or {})),
            download=DownloadConfig(**dict(data.get("download") or {})),
            resume=ResumeConfig(**dict(data.get("resume") or {})),
            output=OutputConfig(**dict(data.get("output") or {})),
            logging=LoggingConfig(**dict(data.get("logging") or {})),
        )