"""
Find the PDF link on a publisher landing page.

Most "gold open access" DOIs do not point at a file. They point at an article page, and
the PDF sits one click away. Without this step every such paper fails validation with
"does not look like a PDF" even though the full text is freely available -- that was the
single largest recoverable failure mode in production.

Three strategies, tried in order, all HTML-only (no JavaScript execution):

1. ``<meta name="citation_pdf_url">`` -- the Google Scholar indexing convention. Almost
   every journal platform emits it, and it names the PDF directly. bepress/Digital Commons
   and EPrints use their own prefixed spelling of the same tag, so accept those too.
2. An Open Journal Systems download link (``.../article/download/<id>/<galley>``). OJS runs
   a large share of small and diamond-OA journals and does not emit citation_pdf_url.
3. Any same-host anchor whose URL looks like a PDF.

Deliberately *not* handled: pages whose download button is a JavaScript postback or sits
behind a CAPTCHA (SciTePress, some ACM pages). Those need a browser, and a systematic
review pipeline should report them as unavailable rather than pretend.
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin, urlparse

# Attribute order varies by platform, so match the tag then pull the two attributes out of
# it independently rather than assuming name-then-content.
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""(\w[\w:-]*)\s*=\s*["']([^"']*)["']""")

_ANCHOR_HREF_RE = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

_OJS_DOWNLOAD_PATH = "/article/download/"

# Platform spellings of the same "here is the PDF" meta tag.
_PDF_META_NAMES = frozenset({
    "citation_pdf_url",
    "bepress_citation_pdf_url",
    "eprints.document_url",
})

# A URL that ends in .pdf, or carries it before a query string.
_LOOKS_LIKE_PDF_RE = re.compile(r"\.pdf(?:$|[?#])", re.IGNORECASE)


def _host(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _registrable_ish(host: str) -> str:
    """Last two labels of a host -- enough to tell 'same publisher' from 'somewhere else'.

    Not a real public-suffix lookup; it only has to reject cross-site redirects, and
    over-matching on a two-label ccTLD is harmless here because the alternative (an exact
    host match) would reject the very common landing-page/files-host split.
    """
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _same_site(a: str, b: str) -> bool:
    return _registrable_ish(_host(a)) == _registrable_ish(_host(b))


def _citation_pdf_url(html: str) -> str | None:
    for tag in _META_TAG_RE.findall(html):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        key = attrs.get("name") or attrs.get("property") or ""
        if key.strip().lower() in _PDF_META_NAMES:
            content = (attrs.get("content") or "").strip()
            if content:
                return unescape(content)
    return None


def _ojs_download_url(html: str) -> str | None:
    """Find an OJS galley-download link.

    Read it off an anchor rather than by matching the path fragment in raw HTML: OJS is
    usually installed under a prefix (``/index.php/<journal>/article/download/...``), and a
    bare fragment match would drop that prefix and resolve to a 404 at the site root.
    """
    for href in _ANCHOR_HREF_RE.findall(html):
        candidate = unescape(href.strip())
        if _OJS_DOWNLOAD_PATH in candidate.lower():
            return candidate
    return None


def _same_host_pdf_anchor(html: str, base_url: str) -> str | None:
    for href in _ANCHOR_HREF_RE.findall(html):
        candidate = unescape(href.strip())
        if not candidate or candidate.startswith(("javascript:", "mailto:", "#")):
            continue
        if not _LOOKS_LIKE_PDF_RE.search(candidate):
            continue
        absolute = urljoin(base_url, candidate)
        if absolute.startswith(("http://", "https://")) and _same_site(absolute, base_url):
            return absolute
    return None


def extract_pdf_url(html: str, base_url: str) -> str | None:
    """Return an absolute PDF URL found on this landing page, or None.

    ``base_url`` must be the URL the HTML was actually served from (after redirects), both
    to resolve relative links and to keep the result on the same site -- following an
    off-site link here would silently download some other publisher's paper.
    """
    if not html:
        return None

    for finder in (_citation_pdf_url, _ojs_download_url):
        found = finder(html)
        if not found:
            continue
        absolute = urljoin(base_url, found)
        if absolute.startswith(("http://", "https://")) and _same_site(absolute, base_url):
            return absolute

    return _same_host_pdf_anchor(html, base_url)
