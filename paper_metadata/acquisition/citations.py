"""
paper_metadata.retrieval.citations
===================================
Fetches citation and reference edges for one or more papers from the
Semantic Scholar Graph API.

Endpoints used
--------------
GET /paper/{paper_id}/citations
GET /paper/{paper_id}/references

Both endpoints share the same offset-based pagination contract:
  - response["next"] is present iff more pages remain; its value is the
    next offset to request.
  - Maximum limit per call is 1000 (used throughout to minimise round trips).
  - The API silently truncates at approximately 9,999 edges per endpoint.

ID normalisation
----------------
The SS Graph API accepts these formats in the path parameter:

  <sha>        40-char hex Semantic Scholar paper ID
  CorpusId:<n> SS numerical corpus ID
  DOI:<doi>    10.xxxx/...
  ARXIV:<id>   e.g. 2106.15928  (bare or versioned)
  MAG:<n>
  ACL:<id>
  PMID:<n>
  PMCID:<n>
  URL:<url>    from semanticscholar.org, arxiv.org, aclweb.org, acm.org, biorxiv.org

_normalise_id() accepts user input in any of the following forms and converts
it to the canonical format the API expects, without any network call:

  Explicit prefixes (case-insensitive on the prefix part):
    "DOI:10.1234/..."   → "DOI:10.1234/..."
    "arxiv:2106.15928"  → "ARXIV:2106.15928"
    "CorpusId:12345"    → "CorpusId:12345"
    "PMID:19872477"     → "PMID:19872477"
    "PMCID:2323736"     → "PMCID:2323736"
    "MAG:112218234"     → "MAG:112218234"
    "ACL:W12-3903"      → "ACL:W12-3903"
    "URL:https://..."   → "URL:https://..."

  Auto-detected bare forms (no explicit prefix):
    40-char hex string  → passed through as SS SHA paper ID
    Looks like a DOI (starts with "10." and contains "/")
                        → "DOI:<value>"
    Looks like an ArXiv ID (NNNN.NNNNN[vN] or legacy cs/YYMMNNN)
                        → "ARXIV:<value>"
    Plain URL string    → "URL:<value>"

  Bare pure-numeric strings are rejected: they are ambiguous across
  CorpusId, PubMed, and MAG namespaces.  Callers must use an explicit
  prefix (e.g. "CorpusId:12345" or "PMID:19872477").
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from ..config.models import CitationGraphConfig, CitationGraphOptions, PaperGraphResult

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_API_PAGE_LIMIT = 1000          # maximum the SS API accepts per call
# The API silently stops returning edges at this cumulative count.  We surface
# this to the caller via the *_truncated flag on PaperGraphResult.
_API_EDGE_CEILING = 9_999

# Recognised explicit prefix tokens, upper-cased for comparison.
_EXPLICIT_PREFIXES: tuple[str, ...] = (
    "DOI",
    "ARXIV",
    "MAG",
    "ACL",
    "PMID",
    "PMCID",
    "CORPUSID",
    "URL",
)

# ── ID Normalisation ──────────────────────────────────────────────────────────

# 40-character lowercase/uppercase hex → SS SHA paper ID
_RE_SS_SHA = re.compile(r"^[0-9a-fA-F]{40}$")

# DOI: starts with "10." and has at least one "/" after the registrant code
_RE_DOI = re.compile(r"^10\.\d{4,}/.+$")

# ArXiv ID formats:
#   Modern:  YYMM.NNNNN or YYMM.NNNNNvN   e.g. 2106.15928, 2106.15928v2
#   Legacy:  subject/YYMMNNN               e.g. cs/0612033, hep-th/9901001
_RE_ARXIV_MODERN = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_RE_ARXIV_LEGACY = re.compile(r"^[a-z\-]+(?:\.[A-Z]{2})?/\d{7}$")

# URL: starts with a recognised scheme
_RE_URL = re.compile(r"^https?://")

# Pure numeric: reject bare numbers
_RE_PURE_NUMERIC = re.compile(r"^\d+$")


def _normalise_id(raw: str) -> str:
    """
    Convert any supported identifier string to the canonical form expected by
    the Semantic Scholar Graph API path parameter.

    Raises ValueError for bare numeric strings (ambiguous namespace) and for
    strings that cannot be classified.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("Empty identifier string.")

    # ── Check for an explicit prefix ──────────────────────────────────────────
    colon_pos = raw.find(":")
    if colon_pos > 0:
        prefix = raw[:colon_pos].upper()
        value = raw[colon_pos + 1 :]

        if prefix in _EXPLICIT_PREFIXES:
            # Normalise only the prefix to its canonical casing; preserve value.
            canonical_prefix = (
                "CorpusId" if prefix == "CORPUSID" else prefix
            )
            return f"{canonical_prefix}:{value}"

        # Colon is present but prefix is unrecognised.  Fall through to
        # auto-detection; the value might be a URL or DOI with a scheme-like
        # prefix that we can still classify.

    # ── Auto-detection ────────────────────────────────────────────────────────

    # Reject bare numerics before any other check.
    if _RE_PURE_NUMERIC.match(raw):
        raise ValueError(
            f"Bare numeric identifier '{raw}' is ambiguous. "
            "Use an explicit prefix: CorpusId:<n>, PMID:<n>, MAG:<n>, or PMCID:<n>."
        )

    if _RE_SS_SHA.match(raw):
        return raw  # SS SHA IDs are passed through unchanged

    if _RE_DOI.match(raw):
        return f"DOI:{raw}"

    if _RE_ARXIV_MODERN.match(raw) or _RE_ARXIV_LEGACY.match(raw):
        return f"ARXIV:{raw}"

    if _RE_URL.match(raw):
        return f"URL:{raw}"

    raise ValueError(
        f"Cannot classify identifier '{raw}'. "
        "Provide an explicit prefix (DOI:, ARXIV:, CorpusId:, PMID:, PMCID:, "
        "MAG:, ACL:, URL:) or a 40-character Semantic Scholar paper ID."
    )


# ── Internal fetch helpers ────────────────────────────────────────────────────

@dataclass
class _FetchResult:
    edges: list[dict] = field(default_factory=list)
    raw_count: int = 0          # total before influential_only filtering
    truncated: bool = False


def _paginate(
    url: str,
    params: dict,
    headers: dict,
    max_results: int | None,
    endpoint_label: str,
    paper_label: str,
) -> _FetchResult:
    """
    Drive offset-based pagination for a single citations or references endpoint.

    Stops when:
      - The response omits the 'next' key (last page).
      - Collected edges reach max_results (if set).
      - Collected edges reach _API_EDGE_CEILING (API will return no more).
    """
    result = _FetchResult()
    page = 0
    params = dict(params)  # defensive copy; caller owns original

    while True:
        page += 1
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as exc:
            logger.error(
                "[%s][%s] page %d — request failed: %s",
                paper_label, endpoint_label, page, exc,
            )
            break

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 10))
            logger.warning(
                "[%s][%s] rate limited — sleeping %ds",
                paper_label, endpoint_label, retry_after,
            )
            time.sleep(retry_after)
            continue

        if response.status_code != 200:
            logger.error(
                "[%s][%s] API error %d: %s",
                paper_label, endpoint_label, response.status_code,
                response.text[:300],
            )
            break

        data = response.json()
        # data["data"] may be absent, null (None), or a list.
        # .get() only substitutes the default when the key is absent;
        # an explicit null value returns None, which is not iterable.
        batch: list[dict] = data.get("data") or []
        result.edges.extend(batch)

        logger.debug(
            "[%s][%s] page %d — batch=%d, total=%d",
            paper_label, endpoint_label, page, len(batch), len(result.edges),
        )

        # Check ceiling before deciding whether to continue.
        if len(result.edges) >= _API_EDGE_CEILING:
            result.truncated = True
            logger.warning(
                "[%s][%s] API edge ceiling (%d) reached — results are truncated.",
                paper_label, endpoint_label, _API_EDGE_CEILING,
            )
            break

        if max_results is not None and len(result.edges) >= max_results:
            break

        if "next" not in data:
            break

        params["offset"] = data["next"]
        # No inter-page sleep here; the between-paper delay in the batch loop
        # is the rate-limit knob.  Intra-paper pages run at network speed.

    result.raw_count = len(result.edges)
    return result


# ── Public entry point ────────────────────────────────────────────────────────

def fetch_paper_graph(
    ids: str | list[str],
    *,
    cfg: CitationGraphConfig,
    ss_api_key: str,
    fetch_citations: bool,
    fetch_references: bool,
    citation_options: CitationGraphOptions,
    reference_options: CitationGraphOptions,
    save_dir: Path | None = None,
) -> list[PaperGraphResult]:
    """
    Fetch citation and/or reference edges for one or more paper identifiers.

    Parameters
    ----------
    ids
        One identifier string or a list of them.  Each is normalised by
        _normalise_id() before the API call.
    cfg
        CitationGraphConfig providing the config-file defaults.
    ss_api_key
        Semantic Scholar API key (empty string for unauthenticated access).
    fetch_citations, fetch_references
        Which endpoints to call.
    citation_options, reference_options
        Per-endpoint overrides; None values fall back to cfg.

    save_dir
        When provided, JSON output for each paper is written to:
          save_dir/citations/<safe_paper_id>.json
          save_dir/references/<safe_paper_id>.json
        Only the endpoint(s) that were fetched are written.  Papers with
        errors are skipped.  Directories are created automatically.

    Returns
    -------
    One PaperGraphResult per input identifier, in input order.
    """
    if isinstance(ids, str):
        ids = [ids]

    headers = {"x-api-key": ss_api_key} if ss_api_key else {}
    results: list[PaperGraphResult] = []

    for i, raw_id in enumerate(ids):
        if i > 0:
            time.sleep(cfg.request_delay)

        # ── Normalise ─────────────────────────────────────────────────────────
        try:
            paper_id_param = _normalise_id(raw_id)
        except ValueError as exc:
            logger.error("ID normalisation failed for '%s': %s", raw_id, exc)
            results.append(_error_result(raw_id, str(exc)))
            continue

        paper_label = paper_id_param  # used only in log messages

        citations_data = _FetchResult()
        references_data = _FetchResult()

        # ── Citations ─────────────────────────────────────────────────────────
        if fetch_citations:
            effective_fields = citation_options.fields or cfg.fields
            effective_max = (
                citation_options.max_results
                if citation_options.max_results is not None
                else cfg.max_results
            )
            params: dict[str, object] = {
                "fields": effective_fields,
                "limit": _API_PAGE_LIMIT,
                "offset": 0,
            }
            if citation_options.publication_date_filter:
                params["publicationDateOrYear"] = citation_options.publication_date_filter

            citations_data = _paginate(
                url=f"{cfg.base_url}/{paper_id_param}/citations",
                params=params,
                headers=headers,
                max_results=effective_max,
                endpoint_label="citations",
                paper_label=paper_label,
            )

            if citation_options.influential_only:
                citations_data.edges = [
                    e for e in citations_data.edges if e.get("isInfluential")
                ]

        # ── References ────────────────────────────────────────────────────────
        if fetch_references:
            effective_fields = reference_options.fields or cfg.fields
            effective_max = (
                reference_options.max_results
                if reference_options.max_results is not None
                else cfg.max_results
            )
            # publication_date_filter is a citations-only parameter; silently
            # ignored here regardless of what reference_options carries.
            params = {
                "fields": effective_fields,
                "limit": _API_PAGE_LIMIT,
                "offset": 0,
            }

            references_data = _paginate(
                url=f"{cfg.base_url}/{paper_id_param}/references",
                params=params,
                headers=headers,
                max_results=effective_max,
                endpoint_label="references",
                paper_label=paper_label,
            )

            if reference_options.influential_only:
                references_data.edges = [
                    e for e in references_data.edges if e.get("isInfluential")
                ]

        # ── Assemble result ───────────────────────────────────────────────────
        # paper_id_param is the normalised form accepted by the API.  It is
        # not the SS SHA unless the caller supplied a SHA; for other formats
        # (DOI:..., ARXIV:...) it retains the prefix.  The field documents
        # the exact identifier that was sent to the API.
        def _extract_citation(e: dict) -> dict:
            p = dict(e.get("citingPaper") or e)
            p["_is_influential"] = bool(e.get("isInfluential"))
            return p

        def _extract_reference(e: dict) -> dict:
            p = dict(e.get("citedPaper") or e)
            p["_is_influential"] = bool(e.get("isInfluential"))
            return p

        results.append(PaperGraphResult(
            input_id=raw_id,
            paper_id=paper_id_param,
            citations=[_extract_citation(e) for e in citations_data.edges],
            references=[_extract_reference(e) for e in references_data.edges],
            citations_fetched=citations_data.raw_count,
            references_fetched=references_data.raw_count,
            citations_truncated=citations_data.truncated,
            references_truncated=references_data.truncated,
            error=None,
        ))

        logger.info(
            "[%s] done — citations=%d (raw=%d, truncated=%s) | "
            "references=%d (raw=%d, truncated=%s)",
            paper_label,
            len(results[-1].citations), citations_data.raw_count, citations_data.truncated,
            len(results[-1].references), references_data.raw_count, references_data.truncated,
        )

    if save_dir is not None:
        _save_graph_results(
            results,
            save_dir,
            fetch_citations=fetch_citations,
            fetch_references=fetch_references,
        )

    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_filename(paper_id: str) -> str:
    """
    Convert a normalised paper ID (which may contain colons, slashes, and
    dots) into a string safe for use as a filename on all platforms.

    Examples
    --------
    "DOI:10.18653/v1/N18-3011"  →  "DOI_10.18653_v1_N18-3011"
    "ARXIV:2106.15928"          →  "ARXIV_2106.15928"
    "649def34f8be52c8b662..."   →  "649def34f8be52c8b662..."  (unchanged)
    """
    # Replace characters that are problematic on Windows/Linux/macOS.
    for ch in (":", "/", "\\", "?", "*", "<", ">", "|", '"'):
        paper_id = paper_id.replace(ch, "_")
    return paper_id


def _citation_status(result: PaperGraphResult, fetched: bool) -> str:
    """
    Return a human-readable status string for one endpoint of one paper.

    Possible values
    ---------------
    "not_requested"   – the caller set citations=False / references=False
    "failed"          – the paper ID could not be resolved (error is set)
    "success"         – edges were fetched and returned
    "empty"           – API responded but returned zero edges (SS has no
                        index for this paper's bibliography / citing papers)
    "truncated"       – edges were fetched but the API ceiling was hit;
                        results are partial
    """
    if not fetched:
        return "not_requested"
    if result.error is not None:
        return "failed"
    return "success"


def _ref_status(result: PaperGraphResult, fetched: bool) -> str:
    if not fetched:
        return "not_requested"
    if result.error is not None:
        return "failed"
    return "success"


def _endpoint_detail(
    result: PaperGraphResult,
    fetched: bool,
    edge_count: int,
    raw_count: int,
    truncated: bool,
    file_path: Path | None,
) -> dict:
    """
    Build the per-endpoint sub-dict for the summary report.
    """
    if not fetched:
        return {"status": "not_requested"}
    if result.error is not None:
        return {"status": "failed", "error": result.error}
    if raw_count == 0:
        return {"status": "empty", "fetched": 0, "returned": 0, "truncated": False, "file": None}
    status = "truncated" if truncated else "success"
    return {
        "status":    status,
        "fetched":   raw_count,
        "returned":  edge_count,
        "truncated": truncated,
        "file":      str(file_path) if file_path else None,
    }


def _save_graph_results(
    results: list[PaperGraphResult],
    save_dir: Path,
    fetch_citations: bool,
    fetch_references: bool,
) -> None:
    """
    Persist citation and reference lists as JSON files under save_dir and
    write a summary report covering every paper's outcome.
    """
    from datetime import datetime

    citations_dir  = save_dir / "citations"
    references_dir = save_dir / "references"
    save_dir.mkdir(parents=True, exist_ok=True)

    paper_rows: list[dict] = []
    aggregate = {
        "citations_success":       0,
        "citations_empty":         0,
        "citations_truncated":     0,
        "citations_failed":        0,
        "citations_not_requested": 0,
        "references_success":       0,
        "references_empty":         0,
        "references_truncated":     0,
        "references_failed":        0,
        "references_not_requested": 0,
        "both_failed":              0,
        "both_success":             0,
    }

    for result in results:
        safe_name   = _safe_filename(result.paper_id or result.input_id)
        cit_file: Path | None = None
        ref_file: Path | None = None

        
        if fetch_citations and result.error is None and result.citations:
            citations_dir.mkdir(parents=True, exist_ok=True)
            cit_file = citations_dir / f"{safe_name}.json"
            _write_json(
                {
                    "input_id":  result.input_id,
                    "paper_id":  result.paper_id,
                    "fetched":   result.citations_fetched,
                    "returned":  len(result.citations),
                    "truncated": result.citations_truncated,
                    "papers":    result.citations,
                },
                cit_file,
            )
            logger.info("Saved citations → %s", cit_file)

        
        if fetch_references and result.error is None and result.references:
            references_dir.mkdir(parents=True, exist_ok=True)
            ref_file = references_dir / f"{safe_name}.json"
            _write_json(
                {
                    "input_id":  result.input_id,
                    "paper_id":  result.paper_id,
                    "fetched":   result.references_fetched,
                    "returned":  len(result.references),
                    "truncated": result.references_truncated,
                    "papers":    result.references,
                },
                ref_file,
            )
            logger.info("Saved references → %s", ref_file)

       
        cit_detail = _endpoint_detail(
            result, fetch_citations,
            len(result.citations), result.citations_fetched,
            result.citations_truncated, cit_file,
        )
        ref_detail = _endpoint_detail(
            result, fetch_references,
            len(result.references), result.references_fetched,
            result.references_truncated, ref_file,
        )

        paper_rows.append({
            "input_id":   result.input_id,
            "paper_id":   result.paper_id,
            "citations":  cit_detail,
            "references": ref_detail,
        })

        
        cs = cit_detail["status"]
        rs = ref_detail["status"]

        aggregate[f"citations_{cs}"]  += 1
        aggregate[f"references_{rs}"] += 1

        if cs == "failed" and rs == "failed":
            aggregate["both_failed"] += 1
        if cs in ("success", "truncated") and rs in ("success", "truncated"):
            aggregate["both_success"] += 1

    
    summary = {
        "generated_at": datetime.now().isoformat(),
        "total_papers": len(results),
        "aggregate":    aggregate,
        "papers":       paper_rows,
    }
    summary_path = save_dir / "fetch_summary.json"
    _write_json(summary, summary_path)
    logger.info("Saved fetch summary → %s", summary_path)


def _write_json(data: dict, filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _error_result(raw_id: str, message: str) -> PaperGraphResult:
    return PaperGraphResult(
        input_id=raw_id,
        paper_id=None,
        citations=[],
        references=[],
        citations_fetched=0,
        references_fetched=0,
        citations_truncated=False,
        references_truncated=False,
        error=message,
    )