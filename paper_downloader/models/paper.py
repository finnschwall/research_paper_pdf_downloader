from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(slots=True)
class PaperRecord:
    paper_key: str
    input_type: str
    input_value: str | None = None

    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    publication_date: str | None = None
    abstract: str | None = None

    semantic_scholar_paper_id: str | None = None
    corpus_id: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    dblp_id: str | None = None
    acl_id: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    mag_id: str | None = None

    local_pdf_path: str | None = None

    #: What kind of record this is, when it is not an ordinary paper -- a meeting abstract, a
    #: poster, a withdrawn preprint. Set by the classify stage from Crossref and OpenAlex
    #: metadata before anything is fetched; None for the overwhelming majority. See
    #: paper_downloader.metadata.record_class.
    record_class: str | None = None
    #: The signal that decided `record_class`, in a sentence. Kept because a terminal class
    #: removes a paper from a review and someone will want to check why.
    record_class_reason: str | None = None
    #: The article has been retracted. Not a reason to stop fetching -- a retracted paper
    #: usually still has a PDF, and a screener needs to see it to exclude it properly.
    retracted: bool = False
    #: Crossref's publication-online date, ``YYYY-MM-DD``. Used to tell "the publisher has
    #: not posted the PDF yet" from "the publisher will not give it to us".
    published_online: str | None = None

    external_ids: dict[str, Any] = field(default_factory=dict)
    source_urls: dict[str, str] = field(default_factory=dict)
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_semantic_scholar_record(
        cls,
        record: dict[str, Any],
        *,
        paper_key: str,
        input_type: str = "metadata_json",
        input_value: str | None = None,
        doi: str | None = None,
        arxiv_id: str | None = None,
        dblp_id: str | None = None,
        acl_id: str | None = None,
        pmid: str | None = None,
        pmcid: str | None = None,
        corpus_id: str | None = None,
    ) -> "PaperRecord":
        authors = []
        for author in record.get("authors") or []:
            if isinstance(author, dict):
                name = _clean_str(author.get("name"))
                if name:
                    authors.append(name)
            else:
                name = _clean_str(author)
                if name:
                    authors.append(name)

        venue_name = None
        venue_short = None
        publication_venue = record.get("publicationVenue")
        if isinstance(publication_venue, dict):
            venue_name = _clean_str(publication_venue.get("name"))
            alternate_names = publication_venue.get("alternate_names") or []
            # prefer the shortest alternate name as it's usually the acronym (e.g. "CVPR")
            short_candidates = [
                _clean_str(n) for n in alternate_names
                if isinstance(n, str) and len(n.strip()) <= 10
            ]
            if short_candidates:
                venue_short = short_candidates[0]

        url = _clean_str(record.get("url"))
        oa_pdf = record.get("openAccessPdf") or {}
        oa_pdf_url = _clean_str(oa_pdf.get("url")) if isinstance(oa_pdf, dict) else None

        return cls(
            paper_key=paper_key,
            input_type=input_type,
            input_value=input_value,
            title=_clean_str(record.get("title")),
            authors=authors,
            year=record.get("year"),
            venue=_clean_str(record.get("venue")) or venue_short or venue_name,
            publication_date=_clean_str(record.get("publicationDate")),
            abstract=_clean_str(record.get("abstract")),
            semantic_scholar_paper_id=_clean_str(record.get("paperId")),
            corpus_id=corpus_id,
            doi=doi,
            arxiv_id=arxiv_id,
            dblp_id=dblp_id,
            acl_id=acl_id,
            pmid=pmid,
            pmcid=pmcid,
            external_ids=dict(record.get("externalIds") or {}),
            source_urls={
                k: v
                for k, v in {
                    "semantic_scholar": url,
                    "open_access_pdf": oa_pdf_url,
                }.items()
                if v
            },
            raw_metadata=dict(record),
        )

    @classmethod
    def from_identifier(
        cls,
        *,
        paper_key: str,
        input_type: str,
        input_value: str,
        doi: str | None = None,
        arxiv_id: str | None = None,
        semantic_scholar_paper_id: str | None = None,
        corpus_id: str | None = None,
        acl_id: str | None = None,
        pmid: str | None = None,
        pmcid: str | None = None,
        mag_id: str | None = None,
    ) -> "PaperRecord":
        return cls(
            paper_key=paper_key,
            input_type=input_type,
            input_value=input_value,
            doi=doi,
            arxiv_id=arxiv_id,
            semantic_scholar_paper_id=semantic_scholar_paper_id,
            corpus_id=corpus_id,
            acl_id=acl_id,
            pmid=pmid,
            pmcid=pmcid,
            mag_id=mag_id,
        )

    @classmethod
    def from_local_pdf(
        cls,
        *,
        paper_key: str,
        pdf_path: str | Path,
        input_value: str | None = None,
    ) -> "PaperRecord":
        resolved = str(Path(pdf_path).resolve())
        return cls(
            paper_key=paper_key,
            input_type="local_pdf",
            input_value=input_value or resolved,
            title=Path(resolved).stem,
            local_pdf_path=resolved,
            raw_metadata={
                "local_pdf_path": resolved,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PaperRecord":
        return cls(**dict(data))