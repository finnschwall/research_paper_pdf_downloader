"""One fetch of a paper's Crossref and OpenAlex record, shared by everything that reads it.

Four places in this library want the same two JSON documents: the record classifier
(`metadata.record_class`), the ``crossref`` and ``openalex`` providers, the
``publisher_landing`` provider, and the OpenAlex content provider. Before this module each
fetched its own copy, so one paper cost three Crossref requests and two OpenAlex ones -- and
the rate limiter made the deployment wait for every one of them.

The cache is a plain dict, process-wide and bounded. Two rules keep it honest:

* Only *answers* are cached -- a work record, or a 404 meaning "this DOI is not in there".
  A timeout or a 500 is not an answer, so the next caller asks again rather than inheriting
  a failure it cannot see.
* The key is the normalised DOI, never the URL, so the same paper looked up by two
  providers with different base URLs is one entry.

`LookupResult.error` is the reason the answer is missing, or None when the answer is
authoritative. Providers report it as their own ``last_reason``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

#: Papers per run are in the thousands; entries are a few kilobytes. Bounded so a long run
#: cannot grow without limit, and cleared oldest-first because a paper is finished with
#: before the next one starts.
_CACHE_MAX = 2048

_crossref: dict[str, dict[str, Any] | None] = {}
_openalex: dict[str, dict[str, Any] | None] = {}


@dataclass(slots=True)
class LookupResult:
    """The work record, or why there is none.

    ``work`` is None both for "the API has never heard of this DOI" (``error`` None) and for
    "we could not ask" (``error`` set). Only the second is worth retrying.
    """
    work: dict[str, Any] | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.work is not None


def normalize_doi(doi: str | None) -> str | None:
    """A DOI in the one shape used as a cache key: lower-cased, no URL prefix, no spaces."""
    if not doi:
        return None
    text = str(doi).strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                   "http://dx.doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.strip()
    return text or None


def _remember(cache: dict[str, Any], key: str, value: Any) -> None:
    if len(cache) >= _CACHE_MAX:
        cache.pop(next(iter(cache)), None)
    cache[key] = value


def _request_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str] | None,
    timeout: tuple[int, int],
    verify: bool,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """(payload, error, is_answer). ``is_answer`` is False when the request itself failed."""
    try:
        response = session.get(
            url, params=params or {}, timeout=timeout, allow_redirects=True, verify=verify,
        )
    except requests.RequestException as exc:
        return None, f"request failed: {exc.__class__.__name__}", False

    if response.status_code == 404:
        return None, None, True
    if response.status_code >= 400:
        return None, f"http {response.status_code}", False

    try:
        payload = response.json()
    except ValueError:
        return None, "invalid json", False

    if not isinstance(payload, dict):
        return None, "unexpected json shape", False
    return payload, None, True


def crossref_work(
    doi: str | None,
    *,
    session: requests.Session,
    timeout: tuple[int, int],
    verify: bool = True,
    base_url: str = "https://api.crossref.org",
) -> LookupResult:
    """The Crossref record for this DOI. Cached; the mailto goes on the session's agent."""
    key = normalize_doi(doi)
    if not key:
        return LookupResult(None, "no doi")
    if key in _crossref:
        return LookupResult(_crossref[key])

    payload, error, is_answer = _request_json(
        session, f"{base_url}/works/{quote(key, safe='')}",
        params=None, timeout=timeout, verify=verify,
    )
    if not is_answer:
        return LookupResult(None, error)

    message = payload.get("message") if isinstance(payload, dict) else None
    work = message if isinstance(message, dict) else None
    _remember(_crossref, key, work)
    return LookupResult(work)


def openalex_work(
    doi: str | None,
    *,
    session: requests.Session,
    timeout: tuple[int, int],
    verify: bool = True,
    api_key: str | None = None,
    base_url: str = "https://api.openalex.org",
) -> LookupResult:
    """The OpenAlex record for this DOI. Cached under the DOI, so the key never varies it."""
    key = normalize_doi(doi)
    if not key:
        return LookupResult(None, "no doi")
    if key in _openalex:
        return LookupResult(_openalex[key])

    params: dict[str, str] = {"include_xpac": "true"}
    if api_key:
        params["api_key"] = api_key

    encoded = quote(f"https://doi.org/{key}", safe=":/")
    payload, error, is_answer = _request_json(
        session, f"{base_url}/works/{encoded}",
        params=params, timeout=timeout, verify=verify,
    )
    if not is_answer:
        return LookupResult(None, error)

    work = payload if isinstance(payload, dict) else None
    _remember(_openalex, key, work)
    return LookupResult(work)


def prime_crossref(doi: str | None, work: dict[str, Any] | None) -> None:
    """Put a record already in hand into the cache, so nobody fetches it again."""
    key = normalize_doi(doi)
    if key:
        _remember(_crossref, key, work)


def prime_openalex(doi: str | None, work: dict[str, Any] | None) -> None:
    key = normalize_doi(doi)
    if key:
        _remember(_openalex, key, work)


def clear_cache() -> None:
    """Forget every cached record. For tests and for a long-lived process between batches."""
    _crossref.clear()
    _openalex.clear()
