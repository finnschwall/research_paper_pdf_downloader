"""What the download stage says when it does not get a PDF.

The pipeline's whole value on a failure is the *reason*, so these tests are mostly about
which of several true statements gets reported.
"""
import pytest

from paper_downloader.config.models import ApiConfig, DownloadConfig
from paper_downloader.core import host_gate
from paper_downloader.core.exceptions import HostBlockedError, NotAPDFError, PDFValidationError
from paper_downloader.download.downloader import PDFDownloader, blame_host_url
from paper_downloader.download.entitlement import not_the_full_article
from paper_downloader.models.paper import PaperRecord
from paper_downloader.pipeline.orchestrator import DownloadOrchestrator, _Hop


@pytest.fixture(autouse=True)
def clean_gate():
    host_gate.reset_for_tests()
    yield
    host_gate.reset_for_tests()


# --- the validation order ------------------------------------------------------------

def test_a_tiny_challenge_page_is_read_before_it_is_measured(tmp_path):
    """The 212-byte Incapsula page used to raise 'too small' and was never looked at."""
    page = tmp_path / "x.pdf"
    body = b'<html><script src="/_Incapsula_Resource?a=b"></script></html>'
    page.write_bytes(body)
    downloader = PDFDownloader(DownloadConfig())
    with pytest.raises(NotAPDFError) as caught:
        downloader._validate_downloaded_file(
            page, content_type="text/html", first_bytes=body[:32],
            size_bytes=len(body), final_url="https://ink.library.smu.edu.sg/x",
        )
    assert host_gate.looks_like_challenge(caught.value.body.encode())


def test_a_genuinely_truncated_pdf_is_still_too_small(tmp_path):
    page = tmp_path / "x.pdf"
    page.write_bytes(b"%PDF-1.4 short")
    with pytest.raises(PDFValidationError, match="too small"):
        PDFDownloader(DownloadConfig())._validate_downloaded_file(
            page, content_type="application/pdf", first_bytes=b"%PDF-1.4",
            size_bytes=14, final_url="https://example.org/x.pdf",
        )


# --- entitlement ---------------------------------------------------------------------

def test_elsevier_first_page_is_caught_by_its_own_header():
    assert not_the_full_article("api.elsevier.com", {"X-ELS-Status": "FIRSTPAGE"})


def test_a_response_with_no_entitlement_header_is_taken_at_face_value():
    """A two-page letter is a real paper; guessing from length would reject it."""
    assert not_the_full_article("api.elsevier.com", {"Content-Type": "application/pdf"}) is None
    assert not_the_full_article("arxiv.org", {"X-ELS-Status": "FIRSTPAGE"}) is None


# --- credentials on the wire ----------------------------------------------------------

def test_the_key_goes_on_the_request_not_in_the_url():
    downloader = PDFDownloader(DownloadConfig(), api_config=ApiConfig(openalex_api_key="oa"))
    sent = {}

    class FakeSession:
        def get(self, url, **kwargs):
            sent.update({"url": url, **kwargs})
            raise RuntimeError("stop here")

    downloader._session = FakeSession()
    with pytest.raises(Exception):
        downloader._get("https://content.openalex.org/works/W1.pdf", referer=None)
    assert sent["url"] == "https://content.openalex.org/works/W1.pdf"
    assert sent["params"] == {"api_key": "oa"}


def test_no_credential_is_sent_to_a_host_that_has_none():
    downloader = PDFDownloader(DownloadConfig(), api_config=ApiConfig(openalex_api_key="oa"))
    sent = {}

    class FakeSession:
        def get(self, url, **kwargs):
            sent.update(kwargs)
            raise RuntimeError("stop here")

    downloader._session = FakeSession()
    with pytest.raises(Exception):
        downloader._get("https://arxiv.org/pdf/1234", referer=None)
    assert sent["params"] is None
    assert not any("api" in key.lower() for key in sent["headers"])


# --- failure reasons -------------------------------------------------------------------

def reason(attempts, providers=(), paper=None, code="download_failed_all_candidates"):
    return DownloadOrchestrator._failure_reason(code, attempts, list(providers), paper=paper)


def test_a_challenge_outranks_everything():
    assert reason([
        {"http_status": 404},
        {"host_blocked": True, "host_challenged": True},
        {"not_a_pdf": True},
    ]) == "client_challenged"


def test_a_rate_refusal_is_not_a_challenge():
    assert reason([{"host_blocked": True}]) == "host_refused_client"


def test_the_deny_list_is_its_own_reason():
    assert reason([{"host_blocked": True, "host_denied": True}]) == "host_denied"


def test_a_recent_article_whose_pdf_url_serves_html_is_not_yet_available():
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=5)).isoformat()
    paper = PaperRecord(paper_key="k", input_type="doi", published_online=recent)
    assert reason(
        [{"not_a_pdf": True, "pdf_url": "https://nature.com/articles/s41598.pdf"}], paper=paper,
    ) == "not_yet_available"


def test_the_same_page_on_an_old_article_is_just_a_page_without_a_link():
    paper = PaperRecord(paper_key="k", input_type="doi", published_online="2019-01-01")
    assert reason(
        [{"not_a_pdf": True, "pdf_url": "https://nature.com/articles/s41598.pdf"}], paper=paper,
    ) == "page_without_link"


def test_an_arxiv_404_is_a_withdrawal():
    assert reason([{"http_status": 404, "source_name": "arxiv"}]) == "withdrawn"


def test_no_candidate_anywhere_is_not_free():
    assert reason([], code="unresolved_no_legal_pdf") == "not_free"


# --- hop bookkeeping ---------------------------------------------------------------------

def test_a_single_hop_records_nothing_extra():
    hop = _Hop(candidate_url="https://example.org/a.pdf")
    assert hop.trail() == {"candidate_url": "https://example.org/a.pdf"}


def test_a_followed_link_and_its_redirect_are_both_recorded():
    """Without this, a Nature page that was followed looked like one that offered no link."""
    hop = _Hop(candidate_url="https://nature.com/articles/s1")
    hop.followed_from = "https://nature.com/articles/s1"
    hop.attempted_url = "https://nature.com/articles/s1.pdf"
    hop.final_url = "https://nature.com/articles/s1"
    assert hop.trail() == {
        "candidate_url": "https://nature.com/articles/s1",
        "followed_from": "https://nature.com/articles/s1",
        "final_url": "https://nature.com/articles/s1",
    }


# --- who gets blamed for a refusal ---------------------------------------------------

class _FakeResponse:
    """Just enough of a `requests.Response` for the attribution path.

    `url` is what requests sets after following redirects, which is the whole point here.
    """

    def __init__(self, *, url, status_code, body=b"", headers=None):
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def iter_content(self, chunk_size=None):
        yield self._body


def test_a_publishers_refusal_is_not_filed_against_the_doi_resolver():
    """The bug that took dx.doi.org out for 48 hours.

    We ask the resolver, the publisher answers, and the publisher's Cloudflare challenge
    used to be recorded under the host we asked -- the entry point for every remaining
    paper in the batch.
    """
    response = _FakeResponse(
        url="https://dl.acm.org/doi/10.1145/3387633",
        status_code=403,
        body=b"<!DOCTYPE html><html><head><title>Just a moment...</title>",
    )
    with pytest.raises(HostBlockedError) as caught:
        PDFDownloader(DownloadConfig())._raise_for_error_status(
            "https://dx.doi.org/10.1145/3387633", response,
        )

    assert caught.value.host == "dl.acm.org"
    assert host_gate.block_state("https://dl.acm.org/doi/x") is not None
    assert host_gate.block_state("https://dx.doi.org/10.1145/3387633") is None


def test_the_requested_url_still_appears_in_the_message():
    """Both facts are needed: which candidate we tried, and who turned it down."""
    response = _FakeResponse(
        url="https://dl.acm.org/doi/10.1145/3387633", status_code=403, body=b"denied",
    )
    with pytest.raises(HostBlockedError) as caught:
        PDFDownloader(DownloadConfig())._raise_for_error_status(
            "https://dx.doi.org/10.1145/3387633", response,
        )
    assert "dl.acm.org refused this server" in str(caught.value)
    assert "https://dx.doi.org/10.1145/3387633" in str(caught.value)


def test_a_response_with_no_final_url_falls_back_to_the_requested_one():
    """A wrong host is still better than no cool-off on a host that is refusing us."""
    response = _FakeResponse(url="", status_code=403, body=b"denied")
    with pytest.raises(HostBlockedError) as caught:
        PDFDownloader(DownloadConfig())._raise_for_error_status(
            "https://esmorwd.org/article/1", response,
        )
    assert caught.value.host == "esmorwd.org"


def test_blame_follows_the_last_hop_not_the_first():
    response = _FakeResponse(url="https://ieeexplore.ieee.org/document/11282905/", status_code=202)
    assert blame_host_url("https://doi.org/10.1109/ACCESS.2025.3641484", response) == (
        "https://ieeexplore.ieee.org/document/11282905/"
    )
