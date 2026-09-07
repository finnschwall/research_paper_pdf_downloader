from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from paper_downloader.core.exceptions import InputParseError
from paper_downloader.metadata.id_recovery import (
    build_paper_key,
    normalize_arxiv_id,
    normalize_doi,
    recover_identifiers_from_record,
)
from paper_downloader.models.paper import PaperRecord


_SEMANTIC_SCHOLAR_HEX_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_DOI_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)?10\.\d{4,9}/\S+$", re.IGNORECASE)
_ARXIV_RE = re.compile(
    r"^(?:https?://arxiv\.org/(?:abs|pdf)/)?(?:arxiv:)?([A-Za-z\-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?$",
    re.IGNORECASE,
)
_PMID_RE = re.compile(r"^(?:pmid:)?(\d{1,8})$", re.IGNORECASE)
_PMCID_RE = re.compile(r"^(?:pmcid:)(\d+)$", re.IGNORECASE)
_MAG_RE = re.compile(r"^mag:(\d+)$", re.IGNORECASE)
_ACL_RE = re.compile(r"^(?:acl:)?([A-Z0-9]+[-_][A-Z0-9]+(?:[-_.][A-Z0-9]+)*)$", re.IGNORECASE)


def infer_identifier_type(value: str) -> str:
    raw = value.strip()
    if _DOI_RE.match(raw):
        return "doi"
    if _ARXIV_RE.match(raw):
        return "arxiv"
    if raw.lower().startswith("corpusid:"):
        return "corpus_id"
    if _SEMANTIC_SCHOLAR_HEX_RE.match(raw):
        return "semantic_scholar_paper_id"
    if raw.lower().startswith("pmcid:"):
        return "pmcid"
    if raw.lower().startswith("pmid:"):
        return "pmid"
    if raw.lower().startswith("mag:"):
        return "mag"
    if raw.lower().startswith("acl:"):
        return "acl_id"
    return "title_or_unknown"


def _local_pdf_key(path: Path) -> str:
    resolved = str(path.resolve())
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]
    return f"localpdf__{digest}"


def _paper_from_local_pdf(path: Path) -> PaperRecord:
    if not path.exists() or not path.is_file():
        raise InputParseError(f"Local PDF file not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise InputParseError(f"Expected a PDF file, got: {path}")

    return PaperRecord.from_local_pdf(
        paper_key=_local_pdf_key(path),
        pdf_path=path,
        input_value=str(path),
    )


def _papers_from_pdf_directory(directory: Path) -> list[PaperRecord]:
    if not directory.exists() or not directory.is_dir():
        raise InputParseError(f"PDF directory not found: {directory}")

    pdf_files = sorted(
        [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf"
        ]
    )
    if not pdf_files:
        raise InputParseError(f"No PDF files found in directory: {directory}")

    return [_paper_from_local_pdf(path) for path in pdf_files]


def _paper_from_identifier(identifier: str) -> PaperRecord:
    identifier_type = infer_identifier_type(identifier)
    raw = identifier.strip()

    normalized_doi = normalize_doi(raw) if identifier_type == "doi" else None
    normalized_arxiv = normalize_arxiv_id(raw) if identifier_type == "arxiv" else None
    semantic_scholar_paper_id = raw if identifier_type == "semantic_scholar_paper_id" else None

    if identifier_type == "corpus_id":
        # Strip "CorpusId:" prefix if present; bare numerics are passed as-is
        corpus_id = raw.split(":", 1)[1] if ":" in raw else raw
    else:
        corpus_id = None

    acl_id = raw.split(":", 1)[1] if identifier_type == "acl_id" else None
    pmid = raw.split(":", 1)[1] if identifier_type == "pmid" else None
    pmcid = raw.split(":", 1)[1] if identifier_type == "pmcid" else None
    mag_id = raw.split(":", 1)[1] if identifier_type == "mag" else None

    paper_key = build_paper_key(
        doi=normalized_doi,
        arxiv_id=normalized_arxiv,
        semantic_scholar_paper_id=semantic_scholar_paper_id,
        corpus_id=corpus_id,
        title=raw if identifier_type == "title_or_unknown" else None,
    )

    return PaperRecord.from_identifier(
        paper_key=paper_key,
        input_type=identifier_type,
        input_value=identifier,
        doi=normalized_doi,
        arxiv_id=normalized_arxiv,
        semantic_scholar_paper_id=semantic_scholar_paper_id,
        corpus_id=corpus_id,
        acl_id=acl_id,
        pmid=pmid,
        pmcid=pmcid,
        mag_id=mag_id,
    )


def _paper_from_metadata_record(record: dict[str, Any]) -> PaperRecord:
    ids = recover_identifiers_from_record(record)
    paper_key = build_paper_key(
        doi=ids.get("doi"),
        arxiv_id=ids.get("arxiv_id"),
        semantic_scholar_paper_id=ids.get("semantic_scholar_paper_id"),
        corpus_id=ids.get("corpus_id"),
        title=record.get("title"),
    )
    return PaperRecord.from_semantic_scholar_record(
        record,
        paper_key=paper_key,
        input_type="metadata_json",
        doi=ids.get("doi"),
        arxiv_id=ids.get("arxiv_id"),
        dblp_id=ids.get("dblp_id"),
        acl_id=ids.get("acl_id"),
        pmid=ids.get("pmid"),
        pmcid=ids.get("pmcid"),
        corpus_id=ids.get("corpus_id"),
        input_value=ids.get("semantic_scholar_paper_id"),
    )


def _load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise InputParseError(f"Failed to parse JSON input file: {path}") from exc


def _parse_json_payload(payload: Any) -> list[PaperRecord]:
    if isinstance(payload, dict):
        if "papers" in payload and isinstance(payload["papers"], list):
            return _parse_json_payload(payload["papers"])
        if "paperIds" in payload and isinstance(payload["paperIds"], list):
            return [_paper_from_identifier(str(item)) for item in payload["paperIds"]]
        if "ids" in payload and isinstance(payload["ids"], list):
            return [_paper_from_identifier(str(item)) for item in payload["ids"]]
        return [_paper_from_metadata_record(payload)]

    if isinstance(payload, list):
        results: list[PaperRecord] = []
        for item in payload:
            if isinstance(item, dict):
                results.append(_paper_from_metadata_record(item))
            elif isinstance(item, str):
                results.append(_paper_from_identifier(item))
            else:
                raise InputParseError(f"Unsupported list item type: {type(item)!r}")
        return results

    raise InputParseError(f"Unsupported JSON payload type: {type(payload)!r}")


def parse_inputs(raw_input: str | Path | dict[str, Any] | Iterable[Any]) -> list[PaperRecord]:
    if isinstance(raw_input, dict):
        return _parse_json_payload(raw_input)

    if isinstance(raw_input, Path):
        if raw_input.exists():
            if raw_input.is_dir():
                return _papers_from_pdf_directory(raw_input)
            if raw_input.is_file() and raw_input.suffix.lower() == ".pdf":
                return [_paper_from_local_pdf(raw_input)]
            if raw_input.is_file() and raw_input.suffix.lower() == ".json":
                return _parse_json_payload(_load_json_file(raw_input))
            raise InputParseError(f"Unsupported input path: {raw_input}")
        raise InputParseError(f"Input path does not exist: {raw_input}")

    if isinstance(raw_input, str):
        candidate_path = Path(raw_input)
        if candidate_path.exists():
            if candidate_path.is_dir():
                return _papers_from_pdf_directory(candidate_path)
            if candidate_path.is_file() and candidate_path.suffix.lower() == ".pdf":
                return [_paper_from_local_pdf(candidate_path)]
            if candidate_path.is_file() and candidate_path.suffix.lower() == ".json":
                return _parse_json_payload(_load_json_file(candidate_path))
            raise InputParseError(f"Unsupported input path: {candidate_path}")
        return [_paper_from_identifier(raw_input)]

    if isinstance(raw_input, Iterable):
        results: list[PaperRecord] = []
        for item in raw_input:
            results.extend(parse_inputs(item))
        return results

    raise InputParseError(f"Unsupported input type: {type(raw_input)!r}")