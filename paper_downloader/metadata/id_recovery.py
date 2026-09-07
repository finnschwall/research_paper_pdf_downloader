from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Any, Mapping

from paper_downloader.metadata.pmc import normalize_pmcid


_DOI_EXTRACT_RE = re.compile(r"(10\.\d{4,9}/\S+)", re.IGNORECASE)
_ARXIV_DIRECT_RE = re.compile(
    r"([A-Za-z\-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
_ARXIV_DOI_RE = re.compile(r"10\.48550/arXiv\.([A-Za-z\-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
_DBLP_CORR_RE = re.compile(r"journals/corr/abs-([0-9]{4})-([0-9]{4,5})(?:v\d+)?", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_doi(raw: Any) -> str | None:
    value = clean_text(raw)
    if not value:
        return None
    value = value.replace("\\", "").strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    match = _DOI_EXTRACT_RE.search(value)
    if not match:
        return None
    return match.group(1).rstrip(" .;,)").lower()


def normalize_arxiv_id(raw: Any) -> str | None:
    value = clean_text(raw)
    if not value:
        return None
    value = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^arxiv:\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\.pdf$", "", value, flags=re.IGNORECASE)
    match = _ARXIV_DIRECT_RE.search(value)
    if not match:
        return None
    arxiv_id = match.group(1)
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id, flags=re.IGNORECASE)
    return arxiv_id


def extract_arxiv_id_from_doi(doi: Any) -> str | None:
    normalized = normalize_doi(doi)
    if not normalized:
        return None
    match = _ARXIV_DOI_RE.search(normalized)
    if not match:
        return None
    return normalize_arxiv_id(match.group(1))


def extract_arxiv_id_from_dblp(dblp_id: Any) -> str | None:
    value = clean_text(dblp_id)
    if not value:
        return None

    corr_match = _DBLP_CORR_RE.search(value)
    if corr_match:
        return normalize_arxiv_id(f"{corr_match.group(1)}.{corr_match.group(2)}")

    if "abs-" in value:
        tail = value.split("abs-", 1)[-1]
        if re.match(r"^\d{4}-\d{4,5}", tail):
            parts = tail.split("-", 1)
            return normalize_arxiv_id(f"{parts[0]}.{parts[1]}")

    return None


def normalize_title(title: Any) -> str:
    raw = clean_text(title) or ""
    lowered = raw.lower()
    lowered = _NON_ALNUM_RE.sub(" ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def title_similarity(left: Any, right: Any) -> float:
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def stable_title_hash(title: Any) -> str:
    normalized = normalize_title(title)
    if not normalized:
        normalized = "unknown"
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "unknown"


def build_paper_key(
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
    semantic_scholar_paper_id: str | None = None,
    corpus_id: str | None = None,
    title: str | None = None,
) -> str:
    if doi:
        return f"doi__{_safe_component(doi.lower())}"
    if arxiv_id:
        return f"arxiv__{_safe_component(arxiv_id)}"
    if semantic_scholar_paper_id:
        return f"ss__{_safe_component(semantic_scholar_paper_id)}"
    if corpus_id:
        return f"corpus__{_safe_component(str(corpus_id))}"
    return f"title__{stable_title_hash(title)}"


def recover_identifiers_from_external_ids(external_ids: Mapping[str, Any] | None) -> dict[str, str | None]:
    ids = dict(external_ids or {})

    doi = normalize_doi(ids.get("DOI"))
    dblp_id = clean_text(ids.get("DBLP"))
    arxiv_id = normalize_arxiv_id(ids.get("ArXiv"))
    if not arxiv_id:
        arxiv_id = extract_arxiv_id_from_doi(doi)
    if not arxiv_id:
        arxiv_id = extract_arxiv_id_from_dblp(dblp_id)

    return {
        "doi": doi,
        "arxiv_id": arxiv_id,
        "dblp_id": dblp_id,
        "acl_id": clean_text(ids.get("ACL")),
        "pmid": clean_text(ids.get("PubMed")),
        # Semantic Scholar spells it "PubMedCentral", with or without the PMC prefix.
        # Digits only here; the Europe PMC provider adds the prefix back.
        "pmcid": normalize_pmcid(ids.get("PubMedCentral")),
        "corpus_id": clean_text(ids.get("CorpusId")),
    }


def recover_identifiers_from_record(record: Mapping[str, Any]) -> dict[str, str | None]:
    external = dict(record.get("externalIds") or {})
    recovered = recover_identifiers_from_external_ids(external)

    doi = recovered["doi"] or normalize_doi(record.get("doi"))
    dblp_id = recovered["dblp_id"] or clean_text(record.get("dblpId"))
    arxiv_id = recovered["arxiv_id"] or normalize_arxiv_id(record.get("arxivId"))
    if not arxiv_id:
        arxiv_id = extract_arxiv_id_from_doi(doi)
    if not arxiv_id:
        arxiv_id = extract_arxiv_id_from_dblp(dblp_id)

    return {
        "semantic_scholar_paper_id": clean_text(record.get("paperId")),
        "corpus_id": recovered["corpus_id"] or clean_text(record.get("corpusId")),
        "doi": doi,
        "arxiv_id": arxiv_id,
        "dblp_id": dblp_id,
        "acl_id": recovered["acl_id"] or clean_text(record.get("aclId")),
        "pmid": recovered["pmid"] or clean_text(record.get("pmid")),
        "pmcid": recovered["pmcid"] or normalize_pmcid(record.get("pmcid")),
    }