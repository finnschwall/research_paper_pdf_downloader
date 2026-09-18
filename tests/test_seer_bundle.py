"""Contract tests for the SEER ingest bundle writer."""
import json

import pytest

from paper_metadata.export.seer_bundle import SCHEMA_VERSION, write_bundle


def prov(**over):
    base = {
        "matched_queries": [], "seed_paper_id": "seed1", "edge_type": "reference",
        "is_influential": False, "input_id": None, "fetch_status": None,
    }
    base.update(over)
    return base


def rec(title, *, paper_id=None, doi=None, venue="", provenance=None, **extra):
    r = {"title": title, "venue": venue, "externalIds": {}, "_provenance": provenance or prov()}
    if paper_id:
        r["paperId"] = paper_id
    if doi:
        r["externalIds"]["DOI"] = doi
    r.update(extra)
    return r


def read(bundle_dir):
    papers = json.load(open(bundle_dir / "papers.json"))
    manifest = json.load(open(bundle_dir / "manifest.json"))
    return papers, manifest


def test_citation_graph_keeps_references_with_no_identifier(tmp_path):
    """The only channel grey literature travels through. Dropping it lost 100%."""
    write_bundle(
        tmp_path, run_type="citation_graph",
        papers=[
            rec("An Indexed Paper", paper_id="abc"),
            rec("A Mathematical Framework", venue="Transformer Circuits Thread"),
        ],
        manifest_extra={"seeds": [], "source_label": "t"}, library_version="test",
    )
    papers, manifest = read(tmp_path)
    assert len(papers) == 2
    assert manifest["counts"]["identity_less"] == 1
    assert "identity_dropped" not in manifest["counts"]
    # the venue survives — it is the whole basis of attributing a grey item
    assert papers[1]["venue"] == "Transformer Circuits Thread"


@pytest.mark.parametrize("run_type", ["keyword_search", "by_id"])
def test_other_run_types_still_drop_them(tmp_path, run_type):
    """A search result with no identifier is a broken result, not grey literature."""
    extra = {"queries": {"q1": "x"}} if run_type == "keyword_search" else {}
    p = dict(edge_type=None, matched_queries=["q1"], fetch_status="found")
    if run_type == "by_id":
        p["input_id"] = "10.1/x"
    write_bundle(
        tmp_path, run_type=run_type,
        papers=[
            rec("Fine", paper_id="abc", provenance=prov(**p)),
            rec("No identifier at all", provenance=prov(**p)),
        ],
        manifest_extra={"source_label": "t", **extra}, library_version="test",
    )
    papers, manifest = read(tmp_path)
    assert len(papers) == 1
    assert manifest["counts"]["identity_dropped"] == 1
    assert "identity_less" not in manifest["counts"]


def test_provenance_carries_the_per_edge_facts(tmp_path):
    """intents and contexts describe the citation, not the cited work."""
    write_bundle(
        tmp_path, run_type="citation_graph",
        papers=[rec("A Work", paper_id="abc", provenance=prov(
            is_influential=True, intents=["methodology"], contexts=["as shown in [3]"]))],
        manifest_extra={"seeds": [], "source_label": "t"}, library_version="test",
    )
    papers, _ = read(tmp_path)
    p = papers[0]["_provenance"]
    assert p["is_influential"] is True
    assert p["intents"] == ["methodology"]
    assert p["contexts"] == ["as shown in [3]"]


def test_schema_major_is_still_one(tmp_path):
    """SEER accepts major 1 only; this change is additive and must stay in it."""
    assert SCHEMA_VERSION.split(".")[0] == "1"


def test_a_structurally_broken_record_still_raises(tmp_path):
    with pytest.raises(ValueError):
        write_bundle(
            tmp_path, run_type="citation_graph",
            papers=[{"title": "no provenance at all"}],
            manifest_extra={"seeds": [], "source_label": "t"}, library_version="test",
        )
