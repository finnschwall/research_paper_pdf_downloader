"""The classify stage's effect on a whole run: what gets skipped, and what still gets tried."""
import json
from pathlib import Path

import pytest

from paper_downloader.config.models import PipelineConfig
from paper_downloader.core import host_gate
from paper_downloader.core.stages import PipelineStage
from paper_downloader.metadata import works
from paper_downloader.pipeline.orchestrator import DownloadOrchestrator
from paper_downloader.resolve.resolver import SourceCandidate, SourceResolver


class NoProvider:
    """A provider that never has anything, so a run that reaches resolution fails cleanly."""
    name = "stub"

    def resolve(self, paper):
        return []


@pytest.fixture(autouse=True)
def clean():
    host_gate.reset_for_tests()
    works.clear_cache()
    yield
    host_gate.reset_for_tests()
    works.clear_cache()


def orchestrator(tmp_path: Path) -> DownloadOrchestrator:
    config = PipelineConfig()
    config.output.root_dir = str(tmp_path)
    return DownloadOrchestrator(
        config, resolver=SourceResolver(config.resolution, providers=[NoProvider()]),
    )


def run_one(tmp_path, *, crossref=None, openalex=None, title="A Paper", doi="10.1234/x"):
    works.prime_crossref(doi, crossref)
    works.prime_openalex(doi, openalex)
    orch = orchestrator(tmp_path)
    try:
        return orch.process_inputs([{
            "paperId": "p1", "title": title, "externalIds": {"DOI": doi},
        }])[0], orch
    finally:
        pass


def test_a_meeting_abstract_is_skipped_before_any_provider_is_asked(tmp_path):
    result, orch = run_one(tmp_path, openalex={"type": "conference-abstract"})
    try:
        assert result.status == "skipped_conference_abstract"
        assert result.record_class == "conference_abstract"
        assert result.failure_reason is None
        assert result.downloaded is False
        # Nothing was resolved and nothing was downloaded: the point of the class.
        assert result.provider_attempts == []
        assert result.download_attempts == []
    finally:
        orch.close()


def test_the_manifest_records_why(tmp_path):
    result, orch = run_one(tmp_path, crossref={"DOI": "10.26226/m.1", "type": "posted-content"}, doi="10.26226/m.1")
    try:
        manifest = json.loads(Path(result.manifest_path).read_text())
        details = manifest["stage_states"][PipelineStage.CLASSIFY_RECORD.value]["details"]
        assert details["record_class"] == "poster"
        assert "10.26226" in details["reason"]
        assert manifest["stage_states"][PipelineStage.DOWNLOAD_PDF.value]["status"] == "skipped"
    finally:
        orch.close()


def test_a_retracted_paper_is_still_fetched(tmp_path):
    result, orch = run_one(
        tmp_path,
        crossref={"type": "journal-article", "update-to": [{"type": "retraction"}]},
    )
    try:
        assert result.retracted is True
        assert result.record_class is None
        # It went on to resolution and failed there, which is what should happen.
        assert result.status == "failed_unresolved_no_legal_pdf"
    finally:
        orch.close()


def test_an_ordinary_paper_is_unaffected(tmp_path):
    result, orch = run_one(tmp_path, crossref={"type": "journal-article"})
    try:
        assert result.record_class is None
        assert result.retracted is False
        assert result.status == "failed_unresolved_no_legal_pdf"
        assert result.failure_reason == "not_free"
    finally:
        orch.close()


def test_the_class_survives_a_resume_without_asking_again(tmp_path):
    """The record has not changed between runs; re-fetching it would be pure cost."""
    result, orch = run_one(tmp_path, openalex={"type": "conference-abstract"})
    orch.close()
    works.clear_cache()          # a fresh process: nothing cached, nothing primed
    second = orchestrator(tmp_path)
    try:
        again = second.process_inputs([{
            "paperId": "p1", "title": "A Paper", "externalIds": {"DOI": "10.1234/x"},
        }])[0]
        assert again.status == "skipped_conference_abstract"
        assert again.record_class_reason == result.record_class_reason
    finally:
        second.close()


def test_a_paper_with_no_doi_is_classified_as_nothing_and_carries_on(tmp_path):
    """No DOI means nothing to classify by. It must not become a reason to skip the paper."""
    orch = orchestrator(tmp_path)
    try:
        result = orch.process_inputs([{
            "paperId": "p2", "title": "A Paper With No DOI", "externalIds": {},
        }])[0]
        assert result.record_class is None
        assert result.status == "failed_unresolved_no_legal_pdf"
    finally:
        orch.close()


def test_candidates_never_carry_a_credential_into_the_manifest(tmp_path):
    """Whatever a provider produces, a manifest is a public document within a deployment."""
    candidate = SourceCandidate(
        source_name="wiley", pdf_url="https://api.wiley.com/onlinelibrary/tdm/v1/articles/10.1002%2Fx",
    )
    assert "token" not in json.dumps(candidate.to_dict()).lower()
