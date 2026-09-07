"""Which secret, if any, a request to a given host must carry.

Three of the routes this library can take are authenticated: OpenAlex's content cache wants
an ``api_key`` query parameter, Wiley's text-and-data-mining API wants a token header, and
Elsevier's article API wants a key header. All three would work just as well if the provider
put the secret straight into the candidate URL -- and that is exactly why they must not.

A ``SourceCandidate`` is written to the paper's manifest, to the run's stats files, and, in
SEER, into ``PaperDocument.source_url``. Anything in the URL is therefore published to
everyone who can read those. So the URL a provider produces is always the clean one, and the
secret is attached here, at the moment of the request, keyed by the host it belongs to.

Two consequences worth knowing:

* A resumed run re-attaches credentials from configuration, so a manifest written by an
  earlier run stays usable and stays clean.
* A credential is only ever sent to the one host it was issued for. There is no fallback and
  no wildcard: an unknown host gets no header and no parameter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

#: Wiley's TDM API. Authorisation is by institutional IP range as well as by token, so a
#: token that works from one network answers 403 from another -- that is the API's design,
#: not a bug here.
WILEY_HOST = "api.wiley.com"

#: OpenAlex's full-text cache. Metered: about a cent per file, with a free daily allowance.
OPENALEX_CONTENT_HOST = "content.openalex.org"

#: Elsevier's Article Retrieval API.
ELSEVIER_HOST = "api.elsevier.com"


@dataclass(frozen=True, slots=True)
class HostCredential:
    """What to add to a request for one host."""
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)

    def names(self) -> list[str]:
        """The header and parameter *names* -- safe to log, unlike the values."""
        return sorted([*self.headers, *(f"?{key}" for key in self.params)])


class CredentialTable:
    """Host -> credential, built once from configuration and read on every request."""

    def __init__(self, entries: dict[str, HostCredential] | None = None) -> None:
        self._entries = dict(entries or {})

    def __bool__(self) -> bool:
        return bool(self._entries)

    def for_url(self, url: str) -> HostCredential | None:
        """The credential for this URL's host, or None. Exact host match only."""
        if not self._entries:
            return None
        try:
            host = (urlparse(url).netloc or "").lower().split("@")[-1].split(":")[0]
        except Exception:
            return None
        if host.startswith("www."):
            host = host[4:]
        return self._entries.get(host)

    def hosts(self) -> list[str]:
        return sorted(self._entries)


def build_credentials(api_config) -> CredentialTable:
    """The credential table this deployment's configuration supports.

    A host with no key configured is simply absent, which is what makes every provider that
    needs one able to switch itself off by asking whether its host is in here.
    """
    entries: dict[str, HostCredential] = {}
    if api_config is None:
        return CredentialTable(entries)

    openalex_key = (getattr(api_config, "openalex_api_key", None) or "").strip()
    if openalex_key:
        entries[OPENALEX_CONTENT_HOST] = HostCredential(params={"api_key": openalex_key})

    wiley_token = (getattr(api_config, "wiley_tdm_token", None) or "").strip()
    if wiley_token:
        entries[WILEY_HOST] = HostCredential(headers={"Wiley-TDM-Client-Token": wiley_token})

    elsevier_key = (getattr(api_config, "elsevier_api_key", None) or "").strip()
    if elsevier_key:
        headers = {"X-ELS-APIKey": elsevier_key}
        inst_token = (getattr(api_config, "elsevier_inst_token", None) or "").strip()
        if inst_token:
            headers["X-ELS-Insttoken"] = inst_token
        entries[ELSEVIER_HOST] = HostCredential(headers=headers)

    return CredentialTable(entries)
