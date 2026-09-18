"""
paper_metadata.export.seer_bundle
==================================
Writes a contract-valid SEER ingest bundle to disk.

Bundle layout (see 00_CONTRACT.md §1):
    <bundle_dir>/
        manifest.json   — run-level metadata
        papers.json     — flat array of SS records with _provenance

Schema version: 1.0
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 1.1 adds two things, both additive: `citation_graph` bundles now keep
# references that carry no identifier (see _validate), and `_provenance` may
# carry `intents` and `contexts`. SEER checks the major version only.
SCHEMA_VERSION = "1.1"

_VALID_RUN_TYPES = {"keyword_search", "by_id", "citation_graph"}
_VALID_EDGE_TYPES = {"citation", "reference"}
_VALID_FETCH_STATUSES = {"found", "not_found", "invalid_id"}
_REQUIRED_PROVENANCE_KEYS = {
    "matched_queries", "seed_paper_id", "edge_type",
    "is_influential", "input_id", "fetch_status",
}


def get_library_version() -> str:
    """Return a short git SHA, falling back to the package __version__."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if sha.returncode == 0:
            return sha.stdout.strip()
    except Exception:
        pass
    try:
        from paper_metadata import __version__
        return __version__
    except Exception:
        return "unknown"


def _has_identity(paper: dict) -> bool:
    if paper.get("paperId"):
        return True
    ext = paper.get("externalIds") or {}
    return bool(ext.get("DOI") or ext.get("ArXiv"))


def _validate(
    run_type: str,
    papers: list[dict],
    manifest_extra: dict,
) -> tuple[list[dict], int]:
    """
    Validate papers against contract §3 STRICT rules.  Returns
    (kept_papers, identity_dropped_count, identity_less_kept_count).
    Raises ValueError on structural violations.
    """
    if run_type not in _VALID_RUN_TYPES:
        raise ValueError(
            f"Invalid run_type {run_type!r}. Must be one of {_VALID_RUN_TYPES}."
        )

    queries = manifest_extra.get("queries", {}) if run_type == "keyword_search" else {}

    kept: list[dict] = []
    identity_dropped = 0
    identity_less = 0

    for i, paper in enumerate(papers):
        prov = paper.get("_provenance")
        if not isinstance(prov, dict):
            raise ValueError(
                f"Record {i}: missing or non-dict '_provenance'."
            )
        missing_keys = _REQUIRED_PROVENANCE_KEYS - prov.keys()
        if missing_keys:
            raise ValueError(
                f"Record {i}: '_provenance' missing keys {missing_keys}."
            )

        if run_type == "keyword_search":
            mq = prov.get("matched_queries")
            if not mq or not isinstance(mq, list) or len(mq) == 0:
                raise ValueError(
                    f"Record {i}: 'matched_queries' must be a non-empty list "
                    f"for run_type='keyword_search'."
                )
            unknown = [k for k in mq if k not in queries]
            if unknown:
                raise ValueError(
                    f"Record {i}: 'matched_queries' contains keys not in "
                    f"manifest.queries: {unknown}."
                )

        elif run_type == "citation_graph":
            if prov.get("seed_paper_id") is None:
                raise ValueError(
                    f"Record {i}: 'seed_paper_id' must be non-null for "
                    f"run_type='citation_graph'."
                )
            if prov.get("edge_type") not in _VALID_EDGE_TYPES:
                raise ValueError(
                    f"Record {i}: 'edge_type' must be one of {_VALID_EDGE_TYPES}."
                )
            if not isinstance(prov.get("is_influential"), bool):
                raise ValueError(
                    f"Record {i}: 'is_influential' must be a bool for "
                    f"run_type='citation_graph'."
                )

        elif run_type == "by_id":
            if prov.get("input_id") is None:
                raise ValueError(
                    f"Record {i}: 'input_id' must be non-null for run_type='by_id'."
                )
            if prov.get("fetch_status") not in _VALID_FETCH_STATUSES:
                raise ValueError(
                    f"Record {i}: 'fetch_status' must be one of "
                    f"{_VALID_FETCH_STATUSES}."
                )

        # Identity: a record with no paperId, DOI or ArXiv id cannot become a
        # paper on its own. What to do with it depends on the run type.
        #
        # For a keyword search or a by-id fetch it is a broken result and is
        # dropped, as it always has been.
        #
        # For a CITATION GRAPH it is the point. Roughly one reference in six
        # comes back as a title and a venue string only, and that is the only
        # channel through which grey literature -- a Transformer Circuits
        # article, a forum post, a lab's write-up -- is visible at all: it is
        # in no index, so the bibliographies of papers we already hold are the
        # only place it appears. Dropping those here lost 100% of that content
        # before the consumer ever saw it. They are kept and counted, and it is
        # the consumer's job to keep them out of its paper table -- SEER stores
        # them as CitedWork rows beside the corpus, never as Papers.
        fetch_status = prov.get("fetch_status")
        is_found = fetch_status == "found" if run_type == "by_id" else True
        if is_found and not _has_identity(paper):
            if run_type != "citation_graph":
                identity_dropped += 1
                logger.warning(
                    "Dropping record %d — no paperId/DOI/ArXiv (title=%r)",
                    i, paper.get("title", "")[:60],
                )
                continue
            identity_less += 1

        kept.append(paper)

    return kept, identity_dropped, identity_less


def write_bundle(
    bundle_dir: Path,
    *,
    run_type: str,
    papers: list[dict],
    manifest_extra: dict,
    library_version: str,
) -> None:
    """
    Validate and write a SEER ingest bundle to bundle_dir.

    Parameters
    ----------
    bundle_dir
        Directory to write into (created if absent).
    run_type
        "keyword_search" | "by_id" | "citation_graph"
    papers
        SS records with _provenance already attached.
    manifest_extra
        Must contain the run-type-specific fields described in 00_CONTRACT.md §2:
        queries, seeds, fetch_params, source_label, counts.
    library_version
        Git SHA or package version string (for audit).
    """
    bundle_dir = Path(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    kept_papers, identity_dropped, identity_less = _validate(
        run_type, papers, manifest_extra)

    if identity_dropped:
        logger.warning("%d records dropped (no usable identifier).", identity_dropped)
    if identity_less:
        logger.info(
            "%d references kept with no identifier — the consumer must not make "
            "papers of them.", identity_less,
        )

    counts = dict(manifest_extra.get("counts") or {})
    counts["total_unique_papers"] = len(kept_papers)
    if identity_dropped:
        counts["identity_dropped"] = identity_dropped
    if identity_less:
        counts["identity_less"] = identity_less

    papers_path = bundle_dir / "papers.json"
    # Write papers first so a missing manifest signals a partial bundle.
    with open(papers_path, "w", encoding="utf-8") as fh:
        json.dump(kept_papers, fh, indent=2, ensure_ascii=False)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "pipeline": "paper_metadata",
        "library_version": library_version,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_type": run_type,
        "source_label": manifest_extra.get("source_label"),
        "fetch_params": manifest_extra.get("fetch_params") or {},
        "queries": manifest_extra.get("queries") or {},
        "seeds": manifest_extra.get("seeds") or [],
        "counts": counts,
        "papers_file": "papers.json",
    }

    manifest_path = bundle_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)

    logger.info(
        "Bundle written to %s — %d papers, run_type=%s",
        bundle_dir, len(kept_papers), run_type,
    )
