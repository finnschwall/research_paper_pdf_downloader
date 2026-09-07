from __future__ import annotations

from enum import Enum


class PipelineStage(str, Enum):
    PARSE_INPUT = "parse_input"
    FETCH_METADATA = "fetch_metadata"
    RECOVER_IDENTIFIERS = "recover_identifiers"
    #: Decide what kind of record this is before spending anything on it. See
    #: paper_downloader.metadata.record_class.
    CLASSIFY_RECORD = "classify_record"
    RESOLVE_SOURCE = "resolve_source"
    DOWNLOAD_PDF = "download_pdf"
    EXTRACT_MARKDOWN = "extract_markdown"
    CAPTION_MARKDOWN = "caption_markdown"


PIPELINE_STAGE_ORDER: tuple[PipelineStage, ...] = (
    PipelineStage.PARSE_INPUT,
    PipelineStage.FETCH_METADATA,
    PipelineStage.RECOVER_IDENTIFIERS,
    PipelineStage.CLASSIFY_RECORD,
    PipelineStage.RESOLVE_SOURCE,
    PipelineStage.DOWNLOAD_PDF,
    PipelineStage.EXTRACT_MARKDOWN,
    PipelineStage.CAPTION_MARKDOWN,
)


def next_stage(stage: PipelineStage) -> PipelineStage | None:
    try:
        index = PIPELINE_STAGE_ORDER.index(stage)
    except ValueError:
        return None
    if index + 1 >= len(PIPELINE_STAGE_ORDER):
        return None
    return PIPELINE_STAGE_ORDER[index + 1]