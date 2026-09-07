"""PubMed Central identifiers, and the one URL that serves their PDFs to this client.

``pmc.ncbi.nlm.nih.gov/articles/<PMCID>/pdf/…`` answers a plain HTTP client with a small
HTML page titled "Preparing to download …" that fetches the file from JavaScript, so it is
not followable here. Europe PMC mirrors the same full text and serves it directly at
``https://europepmc.org/articles/<PMCID>?pdf=render``. Every place that learns a PMCID --
Semantic Scholar's external ids, OpenAlex's ``ids.pmcid``, a PMC landing page URL in any
aggregator's record, the interstitial itself -- therefore routes to Europe PMC.
"""
from __future__ import annotations

import re
from typing import Any

PMC_HOST = "pmc.ncbi.nlm.nih.gov"

_PMCID_RE = re.compile(r"^(?:PMC)?(\d+)$", re.IGNORECASE)
_PMC_URL_RE = re.compile(r"(?:ncbi\.nlm\.nih\.gov|europepmc\.org)/(?:pmc/)?articles?/PMC(\d+)", re.IGNORECASE)

#: What the PMC PDF endpoint serves instead of the document.
_INTERSTITIAL_MARKER = "preparing to download"


def normalize_pmcid(raw: Any) -> str | None:
    """'PMC11751882', 'pmc11751882' or '11751882' -> '11751882'. Anything else -> None."""
    if raw is None:
        return None
    text = str(raw).strip()
    match = _PMCID_RE.match(text)
    return match.group(1) if match else None


def pmcid_from_url(url: Any) -> str | None:
    """The PMCID named in a PMC or Europe PMC article URL, digits only, or None."""
    if not url or not isinstance(url, str):
        return None
    match = _PMC_URL_RE.search(url)
    return match.group(1) if match else None


def europepmc_render_url(pmcid: str) -> str:
    return f"https://europepmc.org/articles/PMC{pmcid}?pdf=render"


def europepmc_landing_url(pmcid: str) -> str:
    return f"https://europepmc.org/articles/PMC{pmcid}"


def is_pmc_interstitial(final_url: str, body: str) -> bool:
    """Did PMC serve its JavaScript download page rather than the PDF?"""
    if PMC_HOST not in (final_url or "").lower():
        return False
    return _INTERSTITIAL_MARKER in (body or "")[:65536].lower()
