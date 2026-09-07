"""Did the server send the article, or a teaser of it?

Publisher APIs answer a request for an article the caller is not entitled to with a valid
PDF containing the first page. Every check a downloader normally makes passes: it starts with
``%PDF-``, the content type is right, the size is plausible, the text is the right paper's.
Nothing downstream can tell it from the real thing, and in a systematic review a one-page
extract silently standing in for a paper's full text is worse than no paper at all.

The only reliable signal is the publisher's own statement of entitlement, in a response
header. So that is what this reads -- never the page count, never the byte size. A response
with no entitlement header is taken at face value, because guessing would reject short but
complete articles (a two-page letter is a real paper).

Elsevier is the only publisher API this library talks to that does this. The check is here,
rather than in ``sources/elsevier.py``, so the download stage can apply it at the moment the
headers arrive without importing a provider.
"""
from __future__ import annotations

#: Header values that mean "what you are holding is not the whole article".
_NON_ENTITLED_VALUES: tuple[str, ...] = (
    "firstpage", "first_page", "first page", "preview", "unentitled", "not entitled",
)

#: Headers, by host suffix, that carry an entitlement statement.
_ENTITLEMENT_HEADERS: dict[str, tuple[str, ...]] = {
    "api.elsevier.com": ("X-ELS-Status", "X-ELS-ResourceVersion", "X-ELS-Entitlement"),
}


def _headers_for(host: str) -> tuple[str, ...]:
    labels = (host or "").split(".")
    for start in range(len(labels)):
        found = _ENTITLEMENT_HEADERS.get(".".join(labels[start:]))
        if found:
            return found
    return ()


def not_the_full_article(host: str, headers) -> str | None:
    """The header that says this is a preview, or None when nothing says so."""
    names = _headers_for(host)
    if not names or not headers:
        return None
    for name in names:
        try:
            value = str(headers.get(name) or "").strip()
        except Exception:
            continue
        lowered = value.lower()
        if value and any(marker in lowered for marker in _NON_ENTITLED_VALUES):
            return f"{name}: {value}"
    return None
