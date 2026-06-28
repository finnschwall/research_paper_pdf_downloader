from __future__ import annotations

import json
import re
import logging
import time
from pathlib import Path

import requests

from ..config.models import MetadataConfig, SearchQuery
from ..deduplication.id_dedup import deduplicate_intra

logger = logging.getLogger(__name__)

# Maps user-facing search_queries.json keys → Semantic Scholar bulk-search API param names.
_SS_PARAM_MAP: dict[str, str] = {
    "date_range":        "publicationDateOrYear",
    "min_citations":     "minCitationCount",
    "publication_types": "publicationTypes",
    "venue":             "venue",
    "fields_of_study":   "fieldsOfStudy",
    "open_access_pdf":   "openAccessPdf",
    "year":              "year",
}

_KNOWN_USER_KEYS = frozenset(_SS_PARAM_MAP)


def load_search_queries(path: str | Path) -> list[SearchQuery]:
    """
    Parse search_queries.json into a list of SearchQuery objects.

    Format expected:
    {
      "defaults": { <user-facing param keys> },
      "queries": {
        "<id>": { "query": "<query string>", <optional overrides> },
        ...
      }
    }
    Unknown keys in defaults or per-query overrides are logged as warnings and skipped.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    defaults: dict = raw.get("defaults", {})
    queries_raw: dict = raw.get("queries", {})

    result: list[SearchQuery] = []
    for qid, qdata in queries_raw.items():
        query_str = qdata.get("query", "")
        if not query_str:
            logger.warning("Query '%s' has no 'query' string — skipping.", qid)
            continue

        overrides = {k: v for k, v in qdata.items() if k != "query"}
        merged = {**defaults, **overrides}

        ss_params: dict = {}
        for k, v in merged.items():
            if k not in _KNOWN_USER_KEYS:
                logger.warning(
                    "Unknown search param '%s' in query '%s' — skipping. "
                    "Known keys: %s",
                    k, qid, ", ".join(sorted(_KNOWN_USER_KEYS)),
                )
                continue
            ss_params[_SS_PARAM_MAP[k]] = v

        # minCitationCount=0 is the SS API default; omit it to keep URLs clean.
        if ss_params.get("minCitationCount", 1) == 0:
            ss_params.pop("minCitationCount", None)

        result.append(SearchQuery(id=qid, query=query_str, ss_params=ss_params))

    return result


def preview_query(sq: SearchQuery, config: MetadataConfig) -> int:
    """
    Return the estimated result count for a single search query without fetching
    any papers. Hits the bulk search endpoint with limit=1 and reads the `total`
    field from the response.
    """
    ss = config.semantic_scholar
    headers = {"x-api-key": config.ss_api_key} if config.ss_api_key else {}
    params: dict = {"query": sq.query, "fields": "paperId", "limit": 1, **sq.ss_params}

    try:
        response = requests.get(ss.bulk_search_url, headers=headers, params=params, timeout=30)
    except requests.RequestException as exc:
        logger.error("[%s] Preview request failed: %s", sq.id, exc)
        return 0

    if response.status_code == 429:
        retry_after = int(response.headers.get("Retry-After", 10))
        logger.warning("[%s] Preview rate limited — sleeping %ds.", sq.id, retry_after)
        time.sleep(retry_after)
        return preview_query(sq, config)

    if response.status_code != 200:
        logger.error("[%s] Preview API error %d: %s", sq.id, response.status_code, response.text[:200])
        return 0

    return response.json().get("total", 0)


def preview_all_queries(
    search_queries: list[SearchQuery],
    config: MetadataConfig,
) -> dict[str, int]:
    """Return {query_id: estimated_count} for every query, without fetching any papers."""
    delay = config.recovery.request_delay
    results: dict[str, int] = {}
    for i, sq in enumerate(search_queries):
        count = preview_query(sq, config)
        results[sq.id] = count
        logger.info("[%s] Preview: ~%d papers", sq.id, count)
        if i < len(search_queries) - 1:
            time.sleep(delay)
    return results


def fetch_all_categories(
    config: MetadataConfig,
    search_queries: list[SearchQuery],
    raw_dir: Path,
) -> dict[str, dict]:
    results: dict[str, dict] = {}

    for sq in search_queries:
        papers = _fetch_category(config, sq)

        _save_json(papers, raw_dir / f"{sq.id}.json")
        logger.info("[%s] Raw saved (%d papers)", sq.id, len(papers))

        unique, dupe_count = deduplicate_intra(papers)
        logger.info(
            "[%s] Raw: %d | Intra dupes removed: %d | After intra: %d",
            sq.id, len(papers), dupe_count, len(unique),
        )

        results[sq.id] = {
            "papers":      unique,
            "raw_count":   len(papers),
            "intra_dupes": dupe_count,
        }

    return results


def _fetch_category(config: MetadataConfig, sq: SearchQuery) -> list[dict]:
    return _run_paginated_query(config, sq)


def _run_paginated_query(config: MetadataConfig, sq: SearchQuery) -> list[dict]:
    ss = config.semantic_scholar
    headers = {"x-api-key": config.ss_api_key} if config.ss_api_key else {}

    params: dict = {
        "query":  sq.query,
        "fields": ss.fields,
        "limit":  1000,
        **sq.ss_params,
    }

    papers: list[dict] = []
    batch_count = 0

    while True:
        batch_count += 1
        logger.info(
            "[%s] Batch %d — fetched so far: %d",
            sq.id, batch_count, len(papers),
        )

        try:
            response = requests.get(
                ss.bulk_search_url, headers=headers, params=params, timeout=30
            )
        except requests.RequestException as exc:
            logger.error("[%s] Request failed: %s", sq.id, exc)
            break

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 10))
            logger.warning("Rate limited. Sleeping %ds.", retry_after)
            time.sleep(retry_after)
            continue

        if response.status_code != 200:
            logger.error(
                "[%s] API error %d: %s",
                sq.id, response.status_code, response.text[:300],
            )
            break

        data = response.json()
        batch = data.get("data", [])
        if not batch:
            break

        papers.extend(batch)

        token = data.get("token")
        if not token:
            break

        params["token"] = token
        time.sleep(1)

    return papers


def _save_json(data: list | dict, filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


_BATCH_LOOKUP_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
_BATCH_SIZE       = 500  # SS-enforced hard maximum per request

_SS_PAPER_ID_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_ARXIV_ID_RE    = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")

_KNOWN_PREFIXES = frozenset({
    "DOI:", "ARXIV:", "MAG:", "ACL:", "PMID:", "PMCID:", "CORPUSID:",
})


def _normalise_paper_id(raw_id: str) -> str:
    raw_id = raw_id.strip()
    if not raw_id:
        raise ValueError("Paper ID must not be empty.")

    upper = raw_id.upper()
    for prefix in _KNOWN_PREFIXES:
        if upper.startswith(prefix):
            return raw_id

    if raw_id.startswith("10.") and "/" in raw_id:
        return f"DOI:{raw_id}"

    if _ARXIV_ID_RE.match(raw_id):
        return f"ARXIV:{raw_id}"

    if _SS_PAPER_ID_RE.match(raw_id):
        return raw_id

    if raw_id.isdigit():
        raise ValueError(
            f"Ambiguous numeric ID '{raw_id}'. "
            f"Provide an explicit prefix: CorpusId:{raw_id}, "
            f"PMID:{raw_id}, or MAG:{raw_id}."
        )

    raise ValueError(f"Unrecognised ID format: '{raw_id}'.")


def _fetch_batch_chunk(
    ids: list[str],
    fields: str,
    headers: dict,
    max_retries: int = 3,
) -> list[dict | None]:
    for attempt in range(max_retries):
        try:
            response = requests.post(
                _BATCH_LOOKUP_URL,
                headers={**headers, "Content-Type": "application/json"},
                params={"fields": fields},
                json={"ids": ids},
                timeout=30,
            )
        except requests.RequestException as exc:
            logger.error("Batch chunk network error: %s", exc)
            return [None] * len(ids)

        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", 10))
            logger.warning(
                "Rate limited on batch chunk — sleeping %ds (attempt %d/%d)",
                wait, attempt + 1, max_retries,
            )
            time.sleep(wait)
            continue

        if response.status_code == 200:
            data = response.json()
            if not isinstance(data, list):
                logger.error(
                    "Unexpected batch response type: expected list, got %s",
                    type(data).__name__,
                )
                return [None] * len(ids)
            if len(data) < len(ids):
                data.extend([None] * (len(ids) - len(data)))
            return data[: len(ids)]

        logger.error(
            "Batch chunk failed | status=%d | body=%s",
            response.status_code,
            response.text[:300],
        )
        return [None] * len(ids)

    logger.error("Batch chunk exhausted %d retries.", max_retries)
    return [None] * len(ids)


def _log_fetch_summary(results: list[dict]) -> None:
    counts: dict[str, int] = {}
    for r in results:
        key = r.get("_fetch_status", "unknown")
        counts[key] = counts.get(key, 0) + 1
    logger.info(
        "fetch_papers_by_ids complete | total=%d | %s",
        len(results),
        "  ".join(f"{k}={v}" for k, v in sorted(counts.items())),
    )


def fetch_papers_by_ids(
    ids: list[str],
    config: MetadataConfig,
) -> list[dict]:
    """
    Fetch full metadata for one or more paper identifiers using the Semantic
    Scholar batch endpoint.  Accepts any mix of SS paperIds, DOIs, ArXiv IDs,
    or explicitly prefixed identifiers (ARXIV:, DOI:, ACL:, MAG:, PMID:,
    PMCID:, CorpusId:).

    The returned list is parallel to the input list and preserves input order.
    Each dict carries two diagnostic fields injected by this function:

        _input_id     – the original identifier string supplied by the caller
        _fetch_status – one of: 'found' | 'not_found' | 'invalid_id'
    """
    if not ids:
        return []

    headers = {"x-api-key": config.ss_api_key} if config.ss_api_key else {}
    fields  = config.semantic_scholar.fields
    results: list[dict] = []

    normalised: list[str | None] = []
    for raw in ids:
        try:
            normalised.append(_normalise_paper_id(raw))
        except ValueError as exc:
            logger.warning("ID normalisation failed — skipping '%s': %s", raw, exc)
            normalised.append(None)

    total_batches = (len(ids) + _BATCH_SIZE - 1) // _BATCH_SIZE

    for batch_idx, start in enumerate(range(0, len(ids), _BATCH_SIZE), start=1):
        end        = min(start + _BATCH_SIZE, len(ids))
        raw_chunk  = ids[start:end]
        norm_chunk = normalised[start:end]

        valid_pairs = [(i, n) for i, n in enumerate(norm_chunk) if n is not None]
        valid_ids   = [n for _, n in valid_pairs]

        logger.info(
            "Batch %d/%d | IDs %d–%d | valid=%d invalid=%d",
            batch_idx, total_batches,
            start + 1, end,
            len(valid_ids),
            len(norm_chunk) - len(valid_ids),
        )

        if valid_ids:
            api_rows = _fetch_batch_chunk(valid_ids, fields, headers)
            pos_to_row: dict[int, dict | None] = {
                chunk_pos: row
                for (chunk_pos, _), row in zip(valid_pairs, api_rows)
            }
        else:
            pos_to_row = {}

        for i, raw in enumerate(raw_chunk):
            if norm_chunk[i] is None:
                results.append({"_input_id": raw, "_fetch_status": "invalid_id"})
                continue

            row = pos_to_row.get(i)
            if not isinstance(row, dict):
                results.append({"_input_id": raw, "_fetch_status": "not_found"})
                continue

            row["_input_id"]     = raw
            row["_fetch_status"] = "found"
            results.append(row)

        if end < len(ids):
            time.sleep(1)

    _log_fetch_summary(results)
    return results
