from __future__ import annotations

from paper_downloader.config.models import PipelineConfig
from paper_downloader.core.exceptions import ConfigurationError


def validate_config(config: PipelineConfig) -> None:
    _validate_resolution(config)
    _validate_download(config)
    _validate_output(config)


def _validate_resolution(config: PipelineConfig) -> None:
    threshold = config.resolution.title_similarity_threshold
    if not (0.0 <= threshold <= 1.0):
        raise ConfigurationError(
            "resolution.title_similarity_threshold must be between 0.0 and 1.0"
        )
    stop_threshold = config.resolution.stop_confidence_threshold
    if not (0.0 <= stop_threshold <= 1.0):
        raise ConfigurationError(
            "resolution.stop_confidence_threshold must be between 0.0 and 1.0"
        )
    if not config.resolution.source_priority:
        raise ConfigurationError("resolution.source_priority must list at least one provider")


def _validate_download(config: PipelineConfig) -> None:
    if config.download.connect_timeout_seconds <= 0:
        raise ConfigurationError("download.connect_timeout_seconds must be > 0")
    if config.download.read_timeout_seconds <= 0:
        raise ConfigurationError("download.read_timeout_seconds must be > 0")
    if config.download.max_retries < 0:
        raise ConfigurationError("download.max_retries must be >= 0")
    if config.download.retry_backoff_seconds < 0:
        raise ConfigurationError("download.retry_backoff_seconds must be >= 0")
    if config.download.min_pdf_bytes <= 0:
        raise ConfigurationError("download.min_pdf_bytes must be > 0")
    if config.download.max_pdf_bytes <= config.download.min_pdf_bytes:
        raise ConfigurationError(
            "download.max_pdf_bytes must be greater than download.min_pdf_bytes"
        )
    if not config.download.user_agent.strip():
        raise ConfigurationError("download.user_agent must not be empty")
    if config.download.landing_page_max_bytes <= 0:
        raise ConfigurationError("download.landing_page_max_bytes must be > 0")


def _validate_output(config: PipelineConfig) -> None:
    if not config.output.root_dir.strip():
        raise ConfigurationError("output.root_dir must not be empty")