"""A bot wall and a rate ban must never be recorded as each other.

The two produce almost identical HTTP responses and want opposite treatment: waiting out a
bot wall accomplishes nothing, and treating a rate ban as unfixable abandons papers that are
one hour away from downloading.
"""
import pytest

from paper_downloader.core import host_gate as hg
from paper_downloader.core.exceptions import HostBlockedError


@pytest.fixture(autouse=True)
def clean_gate():
    hg.reset_for_tests()
    yield
    hg.reset_for_tests()


def test_cloudflare_interstitial_is_a_challenge_not_a_refusal():
    kind = hg.note_response("https://dl.acm.org/doi/10.1/x", 403, b"<html>Just a moment...</html>")
    assert kind == "challenge"
    assert hg.block_state("https://dl.acm.org/y") == ("challenge", "HTTP 403 bot-challenge page")


def test_a_challenge_does_not_climb_the_refusal_ladder():
    """Three walls in a row must not turn into a 48-hour block for a wall that never lifts."""
    for _ in range(3):
        hg.note_response("https://mdpi.com/a", 403, b"<html>Just a moment...</html>")
    kind, reason = hg.block_state("https://mdpi.com/a")
    assert kind == "challenge"
    assert "refusals in 7 days" not in reason


def test_incapsula_page_is_recognised_despite_being_tiny():
    body = b'<html><script src="/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3"></script></html>'
    assert hg.note_response("https://ink.library.smu.edu.sg/x", 200, body) == "challenge"


def test_radware_redirect_is_recognised():
    body = b'<html><meta http-equiv="refresh" content="0;url=https://validate.perfdrive.com/..."></html>'
    assert hg.note_challenge_page("https://iopscience.iop.org/article/x", body.decode()) is True


def test_cloudflare_header_alone_is_enough():
    assert hg.looks_like_challenge(b"x" * 900_000, headers={"cf-mitigated": "challenge"})


def test_a_measured_challenge_host_needs_no_marker():
    """Elsevier's 800 KB bot page carries no boilerplate any scanner would catch."""
    body = b"<html><title>ScienceDirect</title>" + b"x" * 900_000 + b"</html>"
    assert hg.note_response("https://www.sciencedirect.com/science/article/pii/X", 403, body) == "challenge"


def test_an_ordinary_paywall_page_is_neither():
    body = b"<html>" + b"You do not have access to this article. " * 400 + b"</html>"
    assert hg.note_response("https://example.org/article", 403, body) is None
    assert hg.block_state("https://example.org/article") is None


def test_a_small_403_from_an_unknown_host_is_a_refusal():
    assert hg.note_response("https://example.org/a", 403, b"denied") == "refusal"
    kind, _ = hg.block_state("https://example.org/a")
    assert kind == "refusal"


def test_a_refusal_still_escalates():
    for _ in range(3):
        hg._record_refusal("example.org", "HTTP 403")
    assert "3 refusals in 7 days" in hg.block_state("https://example.org/a")[1]


def test_check_blocked_flags_which_kind():
    hg.note_response("https://dl.acm.org/x", 403, b"Just a moment...")
    with pytest.raises(HostBlockedError) as caught:
        hg.check_blocked("https://dl.acm.org/y")
    assert caught.value.challenge is True
    assert caught.value.denied is False


def test_denied_host_is_neither_challenged_nor_refused():
    hg.set_denied_hosts(["acm.org"])
    with pytest.raises(HostBlockedError) as caught:
        hg.check_blocked("https://dl.acm.org/y")
    assert caught.value.denied is True
    assert caught.value.challenge is False


def test_a_captcha_paper_is_not_a_captcha_page():
    """There are papers about CAPTCHAs; the marker list must not match their text."""
    body = b"<html><p>We evaluate captcha solvers and bot detection at scale.</p></html>"
    assert hg.looks_like_challenge(body) is False


def test_an_api_403_is_about_the_article_not_about_us():
    """Wiley answers "you are not entitled to this one" with a small 403.

    Blocking the API on that would make one unentitled article cost every entitled one.
    """
    assert hg.note_response("https://api.wiley.com/onlinelibrary/tdm/v1/articles/x", 403, b"") is None
    assert hg.block_state("https://api.wiley.com/x") is None


def test_an_api_host_is_still_blocked_by_a_real_bot_wall():
    assert hg.note_response(
        "https://api.elsevier.com/content/article/doi/x", 403, b"<html>Just a moment...</html>",
    ) == "challenge"
