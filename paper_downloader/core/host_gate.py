"""One place that decides how fast this process may talk to a host.

Publishers measure requests per second from one IP to one host. A worker count cannot
bound that -- three workers doing 300 ms fetches is ten requests a second -- and it cannot
be stated once either: resolution fires up to twelve provider calls per paper, and which
publisher host a PDF comes from is the *output* of that work, not something a caller could
route on in advance. So the limit belongs at the host, where every caller passes through,
rather than in any one job's pool size.

Two independent knobs per host:

* ``min_interval`` sets the **rate** -- the minimum gap between request *starts*.
* ``concurrency`` sets the **overlap** -- how many may be in flight at once, so one slow
  response does not idle the whole host.

Everything not named in ``_POLICIES`` gets ``DEFAULT_POLICY``: one at a time, one per
second. That is deliberately pessimistic. The fast lane is a whitelist of hosts known to
tolerate us, which is the only direction it is safe to guess in.

The gate also notices when a host stops answering and starts *refusing* -- an edge-network
denial page rather than a paper. One such response blocks the host for
``BLOCK_COOLOFF_SECONDS``, so the rest of a batch fails fast instead of digging the hole
deeper, and the caller can report the papers as retryable rather than as papers that do
not exist.

**In-process only.** The state here is module-level, so it bounds one Python process. SEER
runs a single ``db_worker``, which is what makes that sufficient. Several ``db_worker``
processes would each get their own gate and the effective rate would multiply -- see
docs/subsystems/fulltext-pipeline.md. Making it shared would mean a database-backed token
row; nothing needs that yet.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from paper_downloader.core.exceptions import HostBlockedError

logger = logging.getLogger("paper_downloader")


@dataclass(frozen=True, slots=True)
class HostPolicy:
    #: How many requests to this host may be in flight at once.
    concurrency: int
    #: Minimum seconds between request starts. This, not `concurrency`, is the rate limit.
    min_interval: float


#: One at a time, one per second, for every host not named below. Slow on purpose: the
#: alternative is discovering which publisher bans us next.
DEFAULT_POLICY = HostPolicy(concurrency=1, min_interval=1.0)

#: Keyed on hostname; lookup walks up the dotted suffixes, so an entry for "thecvf.com"
#: also covers "openaccess.thecvf.com". Most specific entry wins.
_POLICIES: dict[str, HostPolicy] = {
    # Preprint servers and free proceedings. Three quarters of every PDF we successfully
    # fetch comes from these and none has ever refused us, so they get the fast lane --
    # still rate-limited, just not to a crawl.
    "arxiv.org": HostPolicy(concurrency=2, min_interval=0.5),
    "aclanthology.org": HostPolicy(concurrency=2, min_interval=0.5),
    "thecvf.com": HostPolicy(concurrency=2, min_interval=0.5),
    "aaai.org": HostPolicy(concurrency=2, min_interval=0.5),

    # Resolver APIs. Their budgets are shared across every job in the deployment, which is
    # the other reason a per-job worker count cannot express the limit.
    "api.openalex.org": HostPolicy(concurrency=1, min_interval=0.2),
    "api.crossref.org": HostPolicy(concurrency=1, min_interval=0.2),
    "api.unpaywall.org": HostPolicy(concurrency=1, min_interval=0.2),
    "api.semanticscholar.org": HostPolicy(concurrency=1, min_interval=1.0),
    "api.core.ac.uk": HostPolicy(concurrency=1, min_interval=0.5),
    "doaj.org": HostPolicy(concurrency=1, min_interval=0.5),
    "zenodo.org": HostPolicy(concurrency=1, min_interval=0.5),
    "ebi.ac.uk": HostPolicy(concurrency=1, min_interval=0.34),
    "ncbi.nlm.nih.gov": HostPolicy(concurrency=1, min_interval=0.34),  # NCBI asks for <=3/s

    # Observed refusing this deployment's IP outright in the September 2026 production run
    # -- the whole site, not just the PDF URLs. Kept in the table so that once the bans age
    # out we do not immediately earn them back.
    "mdpi.com": HostPolicy(concurrency=1, min_interval=3.0),
    "dl.acm.org": HostPolicy(concurrency=1, min_interval=3.0),
    "ieee.org": HostPolicy(concurrency=1, min_interval=3.0),
    "sciencedirect.com": HostPolicy(concurrency=1, min_interval=3.0),
    "acs.org": HostPolicy(concurrency=1, min_interval=3.0),
    "oup.com": HostPolicy(concurrency=1, min_interval=3.0),
    "cambridge.org": HostPolicy(concurrency=1, min_interval=3.0),
}

#: How long a host stays blocked after it refuses us. A cool-off rather than forever: the
#: block is a fact about this moment, and a job an hour from now should get to find out for
#: itself.
BLOCK_COOLOFF_SECONDS = 900.0

#: A refusal body is short -- an Akamai "Access Denied" page is a few hundred bytes. Real
#: "you do not have access to this article" pages are full HTML and much bigger, and those
#: are answers about the paper, not about us.
_SMALL_BODY_BYTES = 4096

#: Statuses worth inspecting for a refusal. 401 is absent on purpose: it is a subscription
#: answer about one article, not an edge denial of the whole site.
_REFUSAL_CANDIDATE_STATUSES = frozenset({202, 403, 429, 503})

#: Markers that identify a challenge or denial page regardless of its size.
_REFUSAL_MARKERS = (
    b"access denied",
    b"attention required",
    b"cf-browser-verification",
    b"just a moment",
    b"unusual traffic",
    b"pardon our interruption",
    b"request unsuccessful. incapsula",
    b"you have been blocked",
    b"enable javascript and cookies to continue",
)

#: The subset of `_REFUSAL_MARKERS` that is unambiguous anti-bot machinery, used when the
#: status was 200 and so carries no signal of its own. "Access denied" is left out: at 200
#: it could plausibly be the text of a paper about access control.
_CHALLENGE_MARKERS = (
    b"attention required",
    b"cf-browser-verification",
    b"just a moment",
    b"pardon our interruption",
    b"request unsuccessful. incapsula",
    b"you have been blocked",
    b"enable javascript and cookies to continue",
)

_ONE_HOP_AT_A_TIME = threading.local()


def host_key(url: str) -> str:
    """The hostname this URL is rate-limited under, with a leading 'www.' dropped.

    Deliberately not a registrable-domain lookup. Collapsing to the last two labels turns
    'core.ac.uk' into 'ac.uk' and would then throttle every UK university as one host.
    """
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return ""
    netloc = netloc.split("@")[-1].split(":")[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def policy_for(host: str) -> HostPolicy:
    """Most specific matching entry in `_POLICIES`, else `DEFAULT_POLICY`."""
    labels = host.split(".")
    for start in range(len(labels)):
        found = _POLICIES.get(".".join(labels[start:]))
        if found is not None:
            return found
    return DEFAULT_POLICY


class _HostState:
    __slots__ = ("policy", "semaphore", "lock", "next_start", "blocked_until",
                 "blocked_reason", "blocked_at")

    def __init__(self, policy: HostPolicy) -> None:
        self.policy = policy
        self.semaphore = threading.BoundedSemaphore(max(1, policy.concurrency))
        self.lock = threading.Lock()
        self.next_start = 0.0
        self.blocked_until = 0.0
        self.blocked_reason = ""
        self.blocked_at = 0.0


_states: dict[str, _HostState] = {}
_states_lock = threading.Lock()


def _state(host: str) -> _HostState:
    with _states_lock:
        state = _states.get(host)
        if state is None:
            state = _HostState(policy_for(host))
            _states[host] = state
        return state


def _claim_slot(state: _HostState) -> float:
    """Reserve this host's next start time and return how long to wait for it.

    The reservation happens under the lock and the sleeping happens outside it, so N
    waiting threads queue up at N * min_interval instead of all sleeping the same amount
    and then firing together.
    """
    interval = state.policy.min_interval
    with state.lock:
        now = time.monotonic()
        start_at = max(now, state.next_start)
        jitter = random.uniform(0.0, 0.3 * interval) if interval else 0.0
        state.next_start = start_at + interval + jitter
        return max(0.0, start_at - now)


@contextmanager
def hold(url: str):
    """Wait for this host's turn, then run the block.

    Re-entrant per thread: `requests` resolves redirects by calling `Session.send` again,
    and a redirect chain is one request as far as the host is concerned. Without this a
    same-host redirect under `concurrency=1` would wait on a semaphore the same thread is
    already holding.
    """
    if getattr(_ONE_HOP_AT_A_TIME, "inside", False):
        yield
        return

    host = host_key(url)
    if not host:
        yield
        return

    state = _state(host)
    _ONE_HOP_AT_A_TIME.inside = True
    try:
        state.semaphore.acquire()
        try:
            wait = _claim_slot(state)
            if wait > 0:
                time.sleep(wait)
            yield
        finally:
            state.semaphore.release()
    finally:
        _ONE_HOP_AT_A_TIME.inside = False


class GatedSession(requests.Session):
    """A `requests.Session` whose every hop passes through `hold()`.

    Overrides `send` rather than `request` so redirects are gated too -- and so that
    callers keep the ordinary Session API and cannot forget to use the gate.
    """

    def send(self, request, **kwargs):  # type: ignore[override]
        with hold(request.url or ""):
            return super().send(request, **kwargs)


def looks_like_refusal(status_code: int, body: bytes) -> bool:
    """Is this response the host refusing us, rather than answering about a paper?"""
    if status_code not in _REFUSAL_CANDIDATE_STATUSES:
        return False
    if status_code == 429:
        # Rate limiting is a refusal we caused and can wait out; `note_retry_after`
        # handles it. Blocking the host outright would be an overreaction.
        return False
    if not body:
        # An empty 202 is IEEE's bot challenge; an empty 403 is an edge denial.
        return True
    if len(body) < _SMALL_BODY_BYTES:
        return True
    lowered = body[:65536].lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def note_response(url: str, status_code: int, body: bytes) -> bool:
    """Record one response and return True if this host is now blocked.

    Called for every response the download path does not accept. One refusal is enough:
    the cost of being wrong is that a few papers get recorded as retryable, and the cost of
    being slow to notice is another ban.
    """
    if not looks_like_refusal(status_code, body):
        return False

    host = host_key(url)
    if not host:
        return False

    state = _state(host)
    with state.lock:
        already = state.blocked_until > time.monotonic()
        state.blocked_until = time.monotonic() + BLOCK_COOLOFF_SECONDS
        state.blocked_reason = f"HTTP {status_code} with a {len(body)}-byte refusal page"
        if not already:
            state.blocked_at = time.monotonic()
    if not already:
        logger.warning(
            "%s is refusing this server (%s) -- skipping it for %.0f min",
            host, state.blocked_reason, BLOCK_COOLOFF_SECONDS / 60,
        )
    return True


def note_challenge_page(url: str, body: str) -> bool:
    """Record a 200-status page that is really a bot challenge; return True if now blocked.

    Cloudflare and Incapsula both serve their interstitials with a 200, so the status says
    nothing and only the boilerplate in the body gives it away. Called when a page we
    reached turned out to offer no PDF link -- the alternative reading of that page, and
    the one that used to win, is "this paper has no downloadable copy".
    """
    lowered = (body or "")[:65536].lower().encode("utf-8", errors="replace")
    if not any(marker in lowered for marker in _CHALLENGE_MARKERS):
        return False

    host = host_key(url)
    if not host:
        return False

    state = _state(host)
    now = time.monotonic()
    with state.lock:
        already = state.blocked_until > now
        state.blocked_until = now + BLOCK_COOLOFF_SECONDS
        state.blocked_reason = "served a bot-challenge page instead of the document"
        if not already:
            state.blocked_at = now
    if not already:
        logger.warning(
            "%s served a bot challenge -- skipping it for %.0f min",
            host, BLOCK_COOLOFF_SECONDS / 60,
        )
    return True


def note_retry_after(url: str, seconds: float) -> None:
    """Push this host's next allowed start out by what its `Retry-After` asked for."""
    host = host_key(url)
    if not host or seconds <= 0:
        return
    state = _state(host)
    with state.lock:
        state.next_start = max(state.next_start, time.monotonic() + seconds)
    logger.info("%s asked for %.0fs before the next request", host, seconds)


def block_reason(url: str) -> str | None:
    """Why this host is currently blocked, or None if it is not."""
    host = host_key(url)
    if not host:
        return None
    with _states_lock:
        state = _states.get(host)
    if state is None:
        return None
    with state.lock:
        if state.blocked_until <= time.monotonic():
            return None
        return state.blocked_reason or "refused an earlier request"


def check_blocked(url: str) -> None:
    """Raise `HostBlockedError` if this host refused us recently."""
    reason = block_reason(url)
    if reason is not None:
        host = host_key(url)
        raise HostBlockedError(
            f"{host} is refusing this server ({reason}) -- not attempting {url}",
            host=host,
        )


def blocked_hosts(*, since: float | None = None) -> list[tuple[str, str]]:
    """Hosts currently blocked, as (host, reason).

    `since` is a `time.monotonic()` reading: pass the one taken when a job started to get
    only the hosts that job discovered, rather than blocks inherited from an earlier one.
    """
    now = time.monotonic()
    with _states_lock:
        items = list(_states.items())
    found = []
    for host, state in items:
        with state.lock:
            if state.blocked_until <= now:
                continue
            if since is not None and state.blocked_at < since:
                continue
            found.append((host, state.blocked_reason))
    return sorted(found)


def reset_for_tests() -> None:
    """Drop all per-host state. Only for tests -- nothing in a run should need this."""
    with _states_lock:
        _states.clear()
