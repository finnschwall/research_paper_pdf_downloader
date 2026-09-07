"""The three credentialed providers, and the CVF bug that cost a whole conference.

The recurring theme: a secret must never reach a URL, because a URL is written to the paper's
manifest, to the run's stats files and, in SEER, to a stored source URL that every account
can read.
"""
import pytest

from paper_downloader.config.models import ApiConfig, DownloadConfig, ResolutionConfig
from paper_downloader.core.credentials import build_credentials
from paper_downloader.models.paper import PaperRecord
from paper_downloader.resolve.resolver import SourceCandidate
from paper_downloader.sources.cvf import _CITATION_PDF_RE, _PDF_LINK_RE
from paper_downloader.sources.elsevier import ElsevierSourceProvider, is_elsevier_doi
from paper_downloader.sources.openalex_content import OpenAlexContentSourceProvider
from paper_downloader.sources.wiley import WileySourceProvider, is_wiley_doi, tdm_url


def paper(doi=None, **kwargs):
    return PaperRecord(paper_key="k", input_type="doi", doi=doi, **kwargs)


# --- CVF -----------------------------------------------------------------------------

@pytest.mark.parametrize("anchor", [
    '<a href="/content/CVPR2025/papers/Peng_Something_CVPR_2025_paper.pdf">pdf</a>',
    '<a href="/content/CVPR2019/papers/x.pdf">[pdf]</a>',
    '<a href="/content/x.pdf"> [pdf] </a>',
    '<a class="btn" href="/content/x.pdf">PDF</a>',
])
def test_cvf_accepts_both_link_texts(anchor):
    """Requiring the square brackets silently dropped every CVPR 2025 paper."""
    assert _PDF_LINK_RE.search(anchor) is not None


def test_cvf_falls_back_to_the_citation_meta_tag():
    html = '<meta name="citation_pdf_url" content="https://openaccess.thecvf.com/a.pdf">'
    assert _CITATION_PDF_RE.search(html).group("href").endswith("a.pdf")


# --- Wiley ---------------------------------------------------------------------------

def test_wiley_prefixes():
    assert is_wiley_doi("10.1002/pro.70031")
    assert is_wiley_doi("10.1111/jan.12345")
    assert not is_wiley_doi("10.1016/j.websem.2024.100822")
    assert not is_wiley_doi(None)


def test_wiley_encodes_the_slash():
    """An unencoded DOI answers an empty 404, which looks exactly like 'no such article'."""
    assert tdm_url("10.1002/pro.70031").endswith("/10.1002%2Fpro.70031")


def _wiley(token="tok"):
    return WileySourceProvider(
        ApiConfig(wiley_tdm_token=token), DownloadConfig(), ResolutionConfig(),
    )


def test_wiley_is_inert_without_a_token():
    provider = _wiley(token=None)
    assert provider.resolve(paper("10.1002/pro.70031")) == []
    assert "WILEY_TDM_TOKEN" in provider.last_reason


def test_wiley_ignores_other_publishers_dois():
    provider = _wiley()
    assert provider.resolve(paper("10.1016/j.x.2024.1")) == []
    assert "not a wiley prefix" in provider.last_reason


def test_wiley_candidate_carries_no_token():
    candidate = _wiley().resolve(paper("10.1002/pro.70031"))[0]
    assert "tok" not in candidate.pdf_url
    assert "tok" not in str(candidate.to_dict())


def test_wiley_does_not_claim_open_access():
    """The API serves subscribed content too; claiming otherwise mislabels paywalled papers."""
    assert _wiley().resolve(paper("10.1002/pro.70031"))[0].asserts_open_access is False


# --- Elsevier ------------------------------------------------------------------------

def test_elsevier_prefixes_and_inertness():
    assert is_elsevier_doi("10.1016/j.websem.2024.100822")
    provider = ElsevierSourceProvider(ApiConfig(), DownloadConfig(), ResolutionConfig())
    assert provider.resolve(paper("10.1016/j.x.1")) == []
    assert "ELSEVIER_API_KEY" in provider.last_reason


def test_elsevier_candidate_carries_no_key():
    provider = ElsevierSourceProvider(
        ApiConfig(elsevier_api_key="secret"), DownloadConfig(), ResolutionConfig(),
    )
    candidate = provider.resolve(paper("10.1016/j.x.1"))[0]
    assert "secret" not in str(candidate.to_dict())


# --- OpenAlex content ----------------------------------------------------------------

def test_openalex_content_is_inert_without_a_key():
    provider = OpenAlexContentSourceProvider(ApiConfig(), DownloadConfig(), ResolutionConfig())
    assert provider.resolve(paper("10.3390/x")) == []
    assert "OPENALEX_API_KEY" in provider.last_reason


def test_openalex_content_url_never_carries_the_key():
    work = {
        "id": "https://openalex.org/W1",
        "has_content": {"pdf": True},
        "content_urls": {"pdf": "https://content.openalex.org/works/W1.pdf?api_key=leaked"},
    }
    assert OpenAlexContentSourceProvider._content_pdf_url(work) == (
        "https://content.openalex.org/works/W1.pdf"
    )


def test_openalex_content_needs_both_the_flag_and_the_url():
    assert OpenAlexContentSourceProvider._content_pdf_url({"has_content": {"pdf": True}}) is None
    assert OpenAlexContentSourceProvider._content_pdf_url(
        {"has_content": {"pdf": False}, "content_urls": {"pdf": "https://x/a.pdf"}}
    ) is None


# --- the credential table ------------------------------------------------------------

def test_credentials_reach_only_their_own_host():
    table = build_credentials(ApiConfig(
        openalex_api_key="oa", wiley_tdm_token="wi", elsevier_api_key="el",
    ))
    assert table.for_url("https://content.openalex.org/works/W1.pdf").params == {"api_key": "oa"}
    assert table.for_url("https://api.wiley.com/x").headers == {"Wiley-TDM-Client-Token": "wi"}
    assert table.for_url("https://api.elsevier.com/x").headers["X-ELS-APIKey"] == "el"
    assert table.for_url("https://evil.example.com/x") is None
    assert table.for_url("https://api.openalex.org/works") is None


def test_an_unconfigured_key_produces_no_entry():
    assert build_credentials(ApiConfig()).hosts() == []


def test_elsevier_institutional_token_rides_along_with_the_key():
    table = build_credentials(ApiConfig(elsevier_api_key="k", elsevier_inst_token="t"))
    assert table.for_url("https://api.elsevier.com/x").headers["X-ELS-Insttoken"] == "t"


def test_credential_names_are_loggable_but_values_are_not():
    names = build_credentials(ApiConfig(wiley_tdm_token="wi")).for_url("https://api.wiley.com/x").names()
    assert names == ["Wiley-TDM-Client-Token"]
    assert "wi" not in " ".join(names)


def test_a_free_copy_is_always_downloaded_before_the_metered_one():
    """OpenAlex's cache costs a cent a file. Europe PMC costs nothing and has the same paper.

    Placing the provider late in the chain is not enough on its own: for a paper whose other
    copies are all on walled hosts, nothing is "good enough" to stop the chain, so the
    provider *is* asked -- and a publisher-version candidate would then outrank an accepted
    manuscript on every other term in the sort key.
    """
    from paper_downloader.resolve.resolver import SourceResolver

    resolver = SourceResolver(ResolutionConfig(), providers=[])
    ranked = resolver._rank([
        SourceCandidate(source_name="europepmc", pdf_url="https://europepmc.org/a?pdf=render",
                        version_type="accepted", host_type="repository", is_direct_pdf=True,
                        confidence=0.87, domain="europepmc.org"),
        SourceCandidate(source_name="openalex_content", pdf_url="https://content.openalex.org/W1.pdf",
                        version_type="publisher", host_type="repository", is_direct_pdf=True,
                        confidence=0.88, domain="content.openalex.org", fallback_only=True),
        SourceCandidate(source_name="publisher_landing", pdf_url="https://mdpi.com/a",
                        version_type="publisher", host_type="publisher", is_direct_pdf=False,
                        confidence=0.50, fallback_only=True, domain="mdpi.com"),
    ])
    assert [c.source_name for c in ranked] == [
        "europepmc", "openalex_content", "publisher_landing",
    ]


def test_the_metered_candidate_never_ends_the_provider_search():
    """Stopping on it would skip the very providers that might have the paper for free."""
    from paper_downloader.resolve.resolver import SourceResolver

    resolver = SourceResolver(ResolutionConfig(), providers=[])
    metered = SourceCandidate(
        source_name="openalex_content", pdf_url="https://content.openalex.org/W1.pdf",
        version_type="publisher", host_type="repository", is_direct_pdf=True,
        confidence=0.95, domain="content.openalex.org", fallback_only=True,
    )
    assert resolver._good_enough([metered]) is False


def test_the_provider_marks_its_candidate_fallback_only():
    work = {"id": "https://openalex.org/W1", "has_content": {"pdf": True},
            "content_urls": {"pdf": "https://content.openalex.org/works/W1.pdf"}}
    provider = OpenAlexContentSourceProvider(
        ApiConfig(openalex_api_key="k"), DownloadConfig(), ResolutionConfig(),
    )
    from unittest.mock import patch
    from paper_downloader.metadata.works import LookupResult
    with patch("paper_downloader.sources.openalex_content.works.openalex_work",
               return_value=LookupResult(work)):
        candidate = provider.resolve(paper("10.3390/x"))[0]
    assert candidate.fallback_only is True
