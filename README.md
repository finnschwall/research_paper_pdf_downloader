# Research paper retrieval

Two pipelines for the two halves of assembling a corpus of papers.

**`paper_metadata`** finds papers: keyword searches against Semantic Scholar, deduplication,
abstract recovery, and citation/reference snowballing.

**`paper_downloader`** gets the PDFs: given a paper's identifier, it asks up to sixteen
sources whether a copy exists, ranks what they offer, and downloads the best one.

Both work from the command line and as a Python library through `paper_data.py`. If you only
want the second one, read [Downloading PDFs](#downloading-pdfs) and
[The providers](#the-providers) and skip the rest.

---

## Contents

- [What this does, and what it will not do](#what-this-does-and-what-it-will-not-do)
- [Install](#install)
- [Keys, and what each one buys](#keys-and-what-each-one-buys)
- [Downloading PDFs](#downloading-pdfs)
- [The providers](#the-providers)
  - [How a paper is resolved](#how-a-paper-is-resolved)
  - [Aggregators](#aggregators)
  - [Venue providers](#venue-providers)
  - [Publisher APIs](#publisher-apis)
  - [Last resorts](#last-resorts)
  - [Choosing which candidate to try first](#choosing-which-candidate-to-try-first)
  - [Turning providers on and off](#turning-providers-on-and-off)
- [When there is no PDF](#when-there-is-no-pdf)
  - [Records with nothing to fetch](#records-with-nothing-to-fetch)
  - [Why the fetch failed](#why-the-fetch-failed)
  - [Bot walls, and why another machine does not help](#bot-walls-and-why-another-machine-does-not-help)
- [Talking to publishers politely](#talking-to-publishers-politely)
- [What a run leaves on disk](#what-a-run-leaves-on-disk)
- [Finding papers: the metadata pipeline](#finding-papers-the-metadata-pipeline)
- [Configuration reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Using this from SEER](#using-this-from-seer)

---

## What this does, and what it will not do

It finds legally readable copies of papers and downloads them. It asks open-access
aggregators, preprint servers, conference sites, institutional repositories and — where a
publisher offers one and you have a key — that publisher's own text-and-data-mining API.

It does not pretend to be a browser. Roughly a third of the papers it cannot get are open
access, sitting on a publisher's own site under a Creative Commons licence, behind an edge
network that refuses every client that is not a real browser. Cloudflare, Imperva, Radware
and several publishers' own filters all do this. Libraries exist that defeat them by
impersonating a browser's TLS fingerprint; using one would violate the "no scripts, spiders
or robots" clause in essentially every institutional licence, so this library does not.

What it does instead is tell you precisely which wall stopped it, and take the sanctioned
route where one exists. For open-access articles that route is usually OpenAlex's full-text
cache; for Wiley and Elsevier it is their TDM APIs; for the rest it is a person with a
browser. See [Bot walls](#bot-walls-and-why-another-machine-does-not-help).

It also refuses to guess. When a DOI turns out to be a meeting abstract, a poster or a
withdrawn preprint, it says so and fetches nothing, rather than reporting a download failure
for a document that does not exist.

---

## Install

Python 3.10 or newer.

```bash
git clone https://github.com/Panzer3232/research_paper_pdf_downloader.git
cd research_paper_pdf_downloader
pip install -r requirements.txt
pip install -e .            # makes `paper_data` importable from anywhere
```

Nothing else is required. Output goes to a `data/` directory created on first run.

---

## Keys, and what each one buys

Every key is optional and every one is free. A missing key switches its provider off; it
never causes an error. Put them in a `.env` file in the directory you run from:

```
SEMANTIC_SCHOLAR_API_KEY=
UNPAYWALL_EMAIL=
CROSSREF_EMAIL=
CORE_API_KEY=
OPENALEX_API_KEY=
WILEY_TDM_TOKEN=
ELSEVIER_API_KEY=
ELSEVIER_INST_TOKEN=
```

| Variable | What it buys | How to get it |
|---|---|---|
| `SEMANTIC_SCHOLAR_API_KEY` | A higher rate limit on metadata lookups. Without it the shared pool throttles hard on large runs. | Request form, free |
| `UNPAYWALL_EMAIL` | **The Unpaywall provider entirely.** Unpaywall returns nothing at all to an anonymous caller. | Any address you own — it is a contact, not an account |
| `CROSSREF_EMAIL` | Crossref's "polite pool": faster, throttled less. | Same, no registration |
| `CORE_API_KEY` | The CORE provider. Without it CORE's download links answer HTTP 400. | Free registration at core.ac.uk |
| `OPENALEX_API_KEY` | A higher OpenAlex rate limit, **and** its full-text cache, which answers 401 without one. | Free, from OpenAlex |
| `WILEY_TDM_TOKEN` | The only working route to a Wiley PDF. | Wiley's TDM page, after a click-through licence |
| `ELSEVIER_API_KEY` | The only working route past ScienceDirect's bot check. | Self-service at dev.elsevier.com, minutes |
| `ELSEVIER_INST_TOKEN` | Extends the Elsevier key to your institution's subscriptions. Optional. | Your library |

Two things worth knowing about the last three.

**They are metered or entitled, not simply "on".** OpenAlex's cache costs about $0.01 per
PDF with roughly a hundred free per day, which is why the library asks it only after every
free route has failed. Wiley's and Elsevier's APIs check your *network* as well as your
token, so the same token works from a subscribing university and not from a laptop at home.

**No key ever enters a URL.** Credentials are attached per host at the moment of the request
([`core/credentials.py`](paper_downloader/core/credentials.py)), because a candidate URL is
written to the paper's manifest, to the run's stats files, and — in SEER — to a stored source
URL that everyone with an account can read.

Shell environment variables take precedence over `.env`. Run your script from the directory
holding the `.env`; both pipelines also search upwards from the working directory.

---

## Downloading PDFs

### From Python

```python
from paper_data import download

results = download("10.1016/j.websem.2024.100822")

for r in results:
    if r.downloaded:
        print(r.pdf_path)
    else:
        print(r.status, "—", r.failure_reason, "—", r.error)
```

`download()` accepts a single identifier, a list of them, or a path to a JSON file. An
identifier can be a DOI, an arXiv id, a Semantic Scholar id, a corpus id, or a prefixed form
(`PMID:`, `PMCID:`, `ACL:`, `MAG:`). It can also be a full Semantic Scholar metadata record,
which is faster — see below.

Walking a list, open the downloader once:

```python
from paper_data import open_downloader

with open_downloader(config=cfg) as dl:
    for paper_id in ids:
        result = dl.download_one(paper_id)
        record(result)                      # your own progress reporting
```

`download()` rebuilds sixteen provider HTTP sessions and re-reads `config.json` on every
call. The handle is **not** thread-safe — the providers hold `requests.Session` objects,
which must not be shared. For parallelism, open one handle per thread; each thread needed
those sessions anyway.

**Pass a metadata record rather than a bare identifier when you have one.** Given only an
identifier, the first thing the pipeline does is fetch the paper's Semantic Scholar record —
and a paper whose metadata step fails is abandoned before arXiv or OpenAlex are asked at all,
so one shared-pool HTTP 429 loses a paper arXiv would have served in a second. A record with
a `title` and `externalIds` skips that call entirely and keeps the identifiers you already
had.

### From the command line

```bash
python main.py --input your_papers.json
python main.py --input "10.1016/j.websem.2024.100822"
python main.py --input your_papers.json --config config.json
python main.py --input your_papers.json --output enriched.json
python main.py --input your_papers.json --stats-dir /path/to/stats --run-label batch_01
```

`--output` writes a copy of the input JSON with `pdf_path`, `download_status` and
`downloaded` added to each record. Without it, one is written next to the input as
`<name>_enriched.json`.

### What comes back

One `DownloadPipelineResult` per input paper, in input order.

| Field | Meaning |
|---|---|
| `downloaded` / `pdf_path` | Whether there is a file, and where |
| `status` | `downloaded`, `already_exists`, `failed_<code>`, or `skipped_<record class>` |
| `failure_reason` | One word for why there is no PDF. `None` on success **and** on every `skipped_` status |
| `record_class` | Set when this DOI is not an ordinary paper — see [Records with nothing to fetch](#records-with-nothing-to-fetch) |
| `retracted` | The article has been retracted. Never stops a fetch; it is a flag for whoever screens the corpus |
| `oa_asserted_by` | Providers that said a free copy exists. Non-empty on a failure means "open access, but this client could not reach it", which is not the same as "not free" |
| `selected_source` | The candidate that won, with its provider, domain, licence and score |
| `provider_attempts` / `download_attempts` | Every provider asked and every URL tried, with the reason each gave |

Re-running is safe. A paper whose PDF is already on disk and whose manifest shows a completed
download is skipped. A paper that previously *failed* is resolved again from scratch, on
purpose: the candidate list on a failed manifest is a list of proven dead URLs, and replaying
it would fail identically and would freeze the paper against every provider added since.

---

## The providers

### How a paper is resolved

A provider answers one question: *is there a copy of this paper, and where?* It returns
candidates — a URL plus what is known about it (direct file or landing page, published
version or preprint, licence, host, and whether the provider claims it is open access).

Resolution runs the providers in the order given by `resolution.source_priority` and **stops
early** once one has produced a candidate good enough that nothing later could beat it: a
direct link to a PDF, on a trusted host, above `stop_confidence_threshold`. With
`prefer_publisher_version` on, only a publisher-version candidate can end the search, so a
preprint-only paper still consults everyone.

Then the download stage tries the candidates in rank order. **When they all fail, the chain
resumes** where the early stop left it, and tries the new candidates too, until a download
succeeds or no provider is left. This matters more than it sounds: in one production run, 12
of 109 failed papers had stopped at an OpenAlex URL that then answered 403, with Europe PMC
three providers further down holding a free copy nobody had asked for.

One more hop is allowed. Most gold-open-access DOIs resolve to an *article page* rather than
a file, so when a candidate serves HTML the page is parsed for the real PDF link
(`citation_pdf_url`, an OJS download link, a same-host `.pdf` anchor) and that link is tried
once. Only links the page itself offers — no crawling, no guessing at URL patterns.

### Aggregators

These index open-access copies across all publishers. They are asked first because a
repository copy costs the publisher nothing and is not behind a bot wall.

| Provider | What it asks | Key | Notes |
|---|---|---|---|
| `metadata_open_access` | The `openAccessPdf` URL already in the Semantic Scholar record | — | Free, no request at all. A `doi.org` URL here is skipped rather than followed — see `publisher_landing` for why |
| `openalex` | OpenAlex's locations for the DOI, then by title | Optional | The broadest coverage of the four. Any PMC id it reports is rewritten to Europe PMC |
| `unpaywall` | Unpaywall's OA locations for the DOI | **Email required** | Off entirely without `UNPAYWALL_EMAIL` |
| `europepmc` | Europe PMC's own full-text holdings | — | Keyed on `inEPMC` + `hasPDF`, *not* on the open-access flag: author manuscripts under a funder mandate are flagged "not OA" and served freely anyway. Gating on the flag dropped exactly those |
| `crossref` | The `link[]` array on the Crossref record | Optional (email) | Skips `similarity-checking` links (iThenticate feeds, all behind the publisher wall) and staging hosts |
| `core` | CORE's index of institutional repositories | **Key required** | Answers HTTP 400 without a key. Titles are searched as `title:("…")`; without the parentheses CORE ignores the quotes — "Attention Is All You Need" matches 3.1 million records instead of 56. CORE has no queryable arXiv-id field at all (`arxivId:` and every variant answer HTTP 500), so it is never asked by arXiv id |
| `doaj` | The Directory of Open Access Journals | — | |
| `zenodo` | Zenodo deposits | — | |

Every PMC identifier, wherever it comes from, is fetched from **Europe PMC** rather than PMC
itself: PMC's own PDF endpoint answers with a "preparing your download" page that needs
JavaScript, and Europe PMC serves the identical file directly.

### Venue providers

These construct a URL from what the paper *is*, without asking an index. They are the
fastest and most reliable routes when they apply.

| Provider | Applies to | How |
|---|---|---|
| `arxiv` | Anything with an arXiv id | Builds the PDF URL directly. Without an id, searches the arXiv API by title and accepts a hit only when the **authors also overlap** — a title alone is how a review ends up citing the wrong paper. Withdrawn preprints are recognised and never offered |
| `acl` | ACL Anthology papers | From the ACL id, or from a `10.18653/v1/…` DOI |
| `cvf` | CVPR, ICCV, ECCV, WACV | Matches the title against the venue's index page, because these papers carry an IEEE DOI that IEEE will not serve. For a computer-vision corpus this is most of the papers |

### Publisher APIs

Where a publisher's website refuses scripts but the publisher offers a sanctioned API, this
is the route. Each is inert until its key is set, and each fires only for DOIs registered to
that publisher — a prefix check that costs nothing for everyone else's papers.

**`wiley`** — `onlinelibrary.wiley.com` is behind a Cloudflare browser challenge, including
for articles published under a Creative Commons licence, so no HTTP client gets a PDF from
it. `api.wiley.com/onlinelibrary/tdm/v1/articles/{DOI}` does serve one. Details that are easy
to get wrong: the DOI's slash must be percent-encoded (an unencoded DOI returns an empty 404,
indistinguishable from "no such article"); the response is a redirect to a single-use signed
URL, so the `api.wiley.com` address is what gets recorded as the source; a missing token gives
400, not 401; and the published rate limit of 60 requests per 10 minutes is the binding one,
so the library waits 10 seconds between requests to this host.

**`elsevier`** — `sciencedirect.com` answers a non-browser with a 403 and an 800 KB HTML page,
and `linkinghub.elsevier.com`, where every Elsevier DOI redirects, serves a JavaScript
redirect to that same wall. The Article Retrieval API returns the PDF instead. Its trap is
that a request you are *not* entitled to comes back as a valid PDF of the article's **first
page only** — right paper, right format, right size, and silently not the paper. The library
reads Elsevier's own entitlement header rather than trying to judge the document, and fails
the candidate honestly. *This provider is unverified: no Elsevier key was available when it
was written. Treat your first run with a real key as its test.*

**`openalex_content`** — OpenAlex caches the full text of the open-access works it indexes and
serves the file from `content.openalex.org`. This is the answer for the largest category of
unreachable paper: an MDPI, ACM or IOP article that is free to read under CC-BY and that no
script can fetch from the publisher. It is a separate provider from `openalex`, placed
near the end of the chain and marked *fallback only*, for one reason — **it costs money**,
about a cent a file against a free daily allowance of roughly a hundred. Both mechanisms are
needed. Its position means the provider is usually not even asked. Its fallback-only mark
means that when it *is* asked — which is exactly the case for a paper whose other copies are
all on walled hosts — its candidate still sorts below every free one, so a repository copy is
downloaded first and the cent is spent only when nothing free worked. The licence on a cached
PDF is the article's own; OpenAlex grants no extra rights, so it only offers files the work
record marks as open access.

### Last resorts

| Provider | What it does | Why it is last |
|---|---|---|
| `broad_search` | A DuckDuckGo search scoped to each trusted domain | Lowest confidence of anything here. On a network where DuckDuckGo is blocked it costs eight connect timeouts per paper and finds nothing — drop it from `source_priority` |
| `publisher_landing` | The publisher's own article page, from Crossref's `resource.primary.URL` | It asks the publisher, which every provider above exists to avoid. But on a university network it recovers subscription articles nobody else can offer: a measured 5 of 110 missing papers in one review, none of which had ever been asked for |

`publisher_landing` is marked *fallback only*: it sorts below every other candidate whatever
it scores, and never ends the provider search. It uses Crossref's `resource.primary.URL`
rather than `https://doi.org/<doi>` deliberately — a `doi.org` URL would be rate-limited under
doi.org rather than under the publisher, would be sent the User-Agent chosen for the wrong
host, and would record "doi.org" as the paper's provenance instead of the page actually read.

### Choosing which candidate to try first

Ranking has two layers.

**A categorical sort** decides first, as a tuple: not-fallback-only, then publisher version,
then direct `.pdf` link, then publisher-hosted. A confirmed version of record outranks a
preprint whatever the scores say.

**A quality score** breaks ties, built from independent additive signals: direct PDF link
`+0.20`; domain in `trusted_domains` `+0.20`; publisher version `+0.30` (or `+0.15` when
`prefer_publisher_version` is off), accepted manuscript `+0.20`, preprint `+0.10` (or `−1.00`
when `allow_preprints` is off); publisher host `+0.10`, repository host `+0.05`; title
similarity at or above the threshold `+0.10`, below it `−0.20`. Finally the provider's own
confidence — from 0.62 for `broad_search` up to 0.97 for the ACL Anthology — contributes at
most `+0.10`, so the pipeline's structural signals always outweigh any provider's opinion of
itself.

Duplicate URLs across providers are collapsed, keeping the higher score. Every candidate's
full breakdown is written to the manifest under `stats.resolution.all_candidates`.

### Turning providers on and off

`resolution.source_priority` is both the order and the enable list. A provider whose name is
not in it is never constructed, so it costs nothing. Unknown names are skipped with a
warning. The shipped default:

```json
["metadata_open_access", "arxiv", "acl", "cvf", "openalex", "unpaywall", "europepmc",
 "crossref", "core", "zenodo", "doaj", "wiley", "elsevier", "broad_search",
 "openalex_content", "publisher_landing"]
```

Free and fast first, publisher APIs after the free aggregators, the metered cache and the
publisher's own website last.

---

## When there is no PDF

Three questions, deliberately kept apart, because they have completely different answers.

### Records with nothing to fetch

Before anything is requested, the pipeline reads the Crossref and OpenAlex records and asks
what kind of thing this DOI *is*. A meeting abstract, a poster deposit, a withdrawn preprint
and an erratum are not papers with missing PDFs; there is no full text anywhere, and no
number of retries will produce one.

When the answer is one of these, resolution and download are skipped entirely — no provider
is asked, no publisher is contacted — and the result comes back as `status =
"skipped_<class>"` with `failure_reason = None`.

| `record_class` | What it is | How it is recognised |
|---|---|---|
| `conference_abstract` | The DOI *is* the abstract | OpenAlex's `conference-abstract` type; or a Crossref supplement issue of an abstract book; or a conference's own numbering in the title (AACR's "Abstract 4137:", ESMO's "316P", ATS session codes) **corroborated** by a supplement or abstract-book container |
| `poster` | A poster deposit | The `10.26226` prefix (Morressier); Crossref `posted-content` with no real subtype |
| `withdrawn` | Pulled by its authors or by the server | arXiv's own withdrawal note |
| `correction` | An erratum or corrigendum | Title, or Crossref's `update-to` |
| `paratext` | Front matter, an issue cover, an editorial board page | OpenAlex's `is_paratext`; Crossref `journal-issue` |
| `not_an_article` | A dataset, a peer-review report, a whole book, a standard | Crossref `type` |

`retracted` is deliberately **not** in this list. A retracted article usually still has a PDF,
and whoever screens the corpus needs to see it in order to exclude it on purpose rather than
by accident — so it is a flag on the result and the fetch proceeds normally.

Three signals were tried and rejected, and should not be added back: Semantic Scholar's
`publicationTypes` (it says `JournalArticle` for abstracts, posters and withdrawn preprints
alike); a single-page page range (MDPI and IOP article numbers look identical to abstract
numbers); and a bare `^Abstract\b` title match (it hits real papers about abstraction).

The two metadata lookups this needs are cached and reused by the `crossref`, `openalex` and
`publisher_landing` providers, so classification costs close to nothing.

### Why the fetch failed

For records that *are* papers, `failure_reason` says what happened. When several attempts
disagree, the first match in this order wins — which is also the order of how little the
result tells you about the paper itself.

| Reason | Meaning | Worth retrying? |
|---|---|---|
| `client_challenged` | A bot wall refused a non-browser client. Says nothing about the paper, the address, or the rate | Not as-is. A publisher API or the OpenAlex cache is the route |
| `host_refused_client` | An edge denial shaped like a rate or IP limit — 429, `Retry-After`, a refusal that clears elsewhere | Later, or from a different network |
| `host_denied` | A host on the configured deny list was never asked | When the configuration changes |
| `transient` | 5xx, timeout, network error, metadata lookup failed | Yes |
| `page_without_link` | A real article page with no followable PDF link: a JavaScript download button, a repository record with nothing deposited | Not by machine |
| `withdrawn` | The arXiv copy was withdrawn | No |
| `not_yet_available` | Published days ago; the publisher serves HTML and no PDF to anyone yet | Yes, in a few weeks — the one reason time alone fixes |
| `not_free` | Every source says closed, or 401/403 on the article with no free copy anywhere | Not without entitlement |
| `dead_link` | Every URL on record answers 404, and the record is not withdrawn | Rarely |

Alongside this, `oa_asserted_by` names the providers that claimed a free copy exists. A
failure with a non-empty `oa_asserted_by` is "open access, and this client could not reach
it" — a fact about the client, not about the paper, and a different thing from `not_free`.

### Bot walls, and why another machine does not help

This was measured rather than assumed. Every refusal was re-tested from a second machine, on
a different network, in a different country, on a different provider. Every one answered
identically. A `curl` control with a Chrome User-Agent got the same refusals as Python
`requests`. **The discriminator is not the address and not the User-Agent — it is that the
client is not a browser.**

| Wall | Hosts | What you see |
|---|---|---|
| Cloudflare | ACM, ACS, Taylor & Francis, OUP, ASME, Wiley, Sage, Emerald, AACR, SSRN | 403, `Cf-Mitigated: challenge`, "Just a moment…" |
| Edge bot filter | MDPI | 403, a 400-byte "Access Denied" page |
| Elsevier's own | ScienceDirect | 403 with an 800 KB HTML page and no marker to find |
| IEEE's own | IEEE Xplore | 202 with an empty body |
| Radware | IOP | HTML, then a redirect to a captcha |
| Imperva Incapsula | some repository mirrors | A 212-byte page that loads a JavaScript challenge |

So moving machines is not a fix, and the library does not treat these as a rate problem. A
challenge gets a flat one-hour skip and its own failure reason; only genuinely rate- or
IP-shaped refusals climb the escalating cool-off below. Feeding a first-request bot wall into
that ladder would have waited out a rate limit that was never there and escalated to a
48-hour self-block for something no amount of waiting can change.

Two of these are worth knowing about specifically. The 212-byte Incapsula page used to be
reported as "file too small to be a valid PDF" — the size check ran before the content check,
so the page was never read, and the obvious-looking fix (raise the size limit) would have
changed nothing. And Elsevier's 800 KB page is too big for a size heuristic and carries no
recognisable boilerplate, so a small list of hosts *measured* to bot-check every script is
consulted as well as the markers.

---

## Talking to publishers politely

Publishers measure requests per second, from one address, to one host. A worker count cannot
express that — three workers doing 300 ms fetches is ten requests a second — and it cannot be
declared in advance either, because *which* publisher host a PDF comes from is the output of
resolution, not something a caller could route on. So the limit lives at the host, in
[`core/host_gate.py`](paper_downloader/core/host_gate.py), and every outbound request in the
library passes through it.

Two knobs per host: a minimum gap between request starts (the rate), and how many may be in
flight at once (the overlap, so one slow response does not idle the host). Anything not named
gets one request per second, one at a time. That default is deliberately pessimistic; the fast
lane is a whitelist of hosts known to tolerate us, which is the only direction it is safe to
guess in.

The gate also notices when a host stops answering and starts *refusing*. Rate- and IP-shaped
refusals are remembered in a JSON file under the data root and escalate — 15 minutes, then
6 hours, then 48 — because a ban is a fact about this client and this host, and a fresh
process should not have to earn it again. Bot challenges are tracked separately, with a flat
one-hour skip and no escalation and no persistence, for the reasons above.

**Rate state is per process.** The limiter is module-level, so it bounds one Python process.
Several processes each get their own gate and the effective rate multiplies.

Publisher API hosts are exempt from refusal-blocking: those answer "you are not entitled to
*this article*" with a small 403, byte-for-byte the shape of an edge denial, and reading it as
one would let a single unentitled article block the API for every entitled paper behind it.

---

## What a run leaves on disk

```
data/
  pdfs/          arxiv__2410.20513.pdf, doi__10.1016_j.websem.2024.100822.pdf, …
  metadata/      the paper record as it was known, including recovered identifiers
  manifests/     per-paper processing history
  reports/       download_stats_<UTC timestamp>_{full.json,short.json,short.csv}
  host_refusals.json
```

Files are named by **paper key**, derived from the best available identifier: `doi__…`,
`arxiv__…`, `ss__…`, `corpus__…`.

A **manifest** is the per-paper record of what happened: every stage with its status and
timestamps, every provider asked and what it said, every URL tried and how it failed, and the
candidate that won. It is also the resume state. A failed attempt records not just the URL
the candidate named but the link that was followed off a landing page and where the request
finally ended, so a two-hop failure is legible afterwards — without that, a Nature article
page that *had* been followed to a `.pdf` link that bounced straight back looked exactly like
one that offered no link at all.

**Stats** are written once per run, timestamped so no run overwrites another: `_full.json`
with everything, `_short.json` with one row per paper, `_short.csv` for a spreadsheet.

---

## Finding papers: the metadata pipeline

`paper_metadata` builds the corpus that `paper_downloader` then fetches. Five stages:

1. **Bulk fetch** — each query in `search_queries.json` is run against Semantic Scholar's
   bulk endpoint, paginated to exhaustion. → `raw/`
2. **Deduplicate by id** — within each query, then across queries in priority order, so each
   paper ends up in exactly one bucket. → `final/`
3. **Deduplicate by title** — catches the same paper under two ids (preprint and published
   version); keeps the more-cited one. Duplicate reports are written as CSV. →
   `final_title_deduped/`
4. **Recover abstracts by API** — arXiv, OpenAlex, PubMed, ACL, Europe PMC, Crossref, CORE
   and Semantic Scholar in turn, each verified by title similarity. →
   `final_recovered_abstract/`
5. **Recover abstracts by scraping** — publisher-specific parsers for Springer, Nature, IEEE,
   Elsevier, Wiley, Taylor & Francis, OUP, CUP, Frontiers, MDPI, ACM, PLOS, AAAI and IJCAI,
   with a JSON-LD fallback. → `publisher_scraped/`

### Queries

`search_queries.json` has `defaults` (applied to every query) and `queries` (a map of query id
to entry, each with a `"query"` string and any overrides). Query ids become output filenames,
and their order sets deduplication priority. The Semantic Scholar bulk search supports `|`
(OR), `+` (AND) and quoted phrases.

```json
{
  "defaults": {
    "date_range": "2020-01-01:",
    "min_citations": 0,
    "publication_types": "JournalArticle,Conference,Dataset,Study"
  },
  "queries": {
    "1_xai_llm": { "query": "(\"explainable AI\" | XAI) + (LLM | \"large language model\")" },
    "2_saes_llm": { "query": "(\"sparse autoencoder\" | SAE) + (LLM | transformer)",
                    "min_citations": 5 }
  }
}
```

Parameter keys: `date_range` (`publicationDateOrYear`), `min_citations`, `publication_types`,
`venue`, `fields_of_study`, `open_access_pdf`, `year`. Each query is atomic — for the same
topic with two date ranges, write two entries.

### Preview, run, audit

```bash
# 1. How many results will each query surface? One light request per query, no data fetched.
python -m paper_metadata.export preview --search-queries-path paper_metadata/search_queries.json

# 2. Run it.
python -m paper_metadata.export keyword --base-dir /data/myproject --label "xai-sweep-june"

# 3. Look at what past runs did.
python -m paper_metadata.export list-runs --base-dir /data/myproject
```

The same three in Python: `preview_queries()`, `fetch_metadata()`, `list_runs()`. Also
`sample_queries(n=20)` for the first few papers per query as a sanity check, and
`recover_abstracts(input_dir=…)` to re-run stages 4 and 5 over an existing run.

Each run creates `<base_dir>/runs/<timestamp>_<label>/` holding one directory per stage, a
`reports/` directory, a snapshot of the exact queries used, and `run.json` recording the id,
label, timings, status and per-query counts. That snapshot is what makes a run reproducible
and is what PRISMA documentation needs.

### By id, and by citation graph

```python
from paper_data import fetch_papers_by_id, fetch_citations_and_references

papers = fetch_papers_by_id(["2106.15928", "DOI:10.18653/v1/N18-3011"], api_recovery=True)

results = fetch_citations_and_references(
    "649def34f8be52c8b66281af98ae884c09aef38b",
    citations=True, references=True, influential_only=True, max_results=200,
    save_dir="/data/snowball",
)
```

Identifiers are auto-detected: 40-character hex is a Semantic Scholar id, `10.…` a DOI,
`2106.15928` or `cs/0612033` an arXiv id, a URL a URL. Explicit prefixes (`DOI:`, `ARXIV:`,
`CorpusId:`, `PMID:`, `PMCID:`, `MAG:`, `ACL:`) also work. **Bare numbers are rejected** —
they are ambiguous across corpus, PubMed and MAG ids, so write `CorpusId:12345678`.

`fetch_papers_by_id` returns one record per input with `_input_id` and `_fetch_status`
(`found` / `not_found` / `invalid_id`). `fetch_citations_and_references` returns one
`PaperGraphResult` per seed; the API silently truncates at about 9,999 edges per endpoint, and
`citations_truncated` / `references_truncated` tell you when that happened. For per-endpoint
control pass `citation_options` / `reference_options`; mixing those with the shorthand
keywords raises rather than silently ignoring one of them.

---

## Configuration reference

`config.json` next to `main.py` controls the downloader. Pass another with `--config`.

| Field | What it controls |
|---|---|
| `resolution.source_priority` | Which providers run, in what order. Both the enable list and the order |
| `resolution.stop_when_confident` / `stop_confidence_threshold` | The early stop described in [How a paper is resolved](#how-a-paper-is-resolved) |
| `resolution.prefer_publisher_version` / `allow_preprints` | Ranking preferences. `prefer_publisher_version` also narrows what may end the provider search |
| `resolution.title_similarity_threshold` | How close a title match must be for a title-based lookup to count. Default 0.90 |
| `resolution.trusted_domains` | Hosts worth `+0.20` in scoring, and the only ones that may end the search early |
| `download.max_retries` / `retry_backoff_seconds` | Retries per URL, with exponential backoff — applied only to failures that could go the other way: connection errors, timeouts, 429, 5xx. A 401/403/404 is an answer, not a glitch, and is never retried |
| `download.landing_page_fallback` / `landing_page_max_bytes` | The one-hop landing-page follow, and how much of such a page to read |
| `download.denied_hosts` | Hosts never to contact, by dotted suffix (`acm.org` covers `dl.acm.org`). Their candidates are kept but tried last and failed without a request, so those papers stay *retryable* rather than being recorded as having no copy |
| `download.min_pdf_bytes` / `max_pdf_bytes` / `allowed_content_types` | What counts as a PDF |
| `output.root_dir` | Where everything is written. **Set this to an absolute path** if your process's working directory is not fixed |
| `resume.*` | Stage skipping and existing-file verification |

`paper_metadata/config.json` controls the other pipeline. The three fields worth setting:
`output.base_dir` (where `runs/` goes), `search_queries_path`, and
`semantic_scholar.fields` (which metadata columns to request).

---

## Troubleshooting

**Everything is `not_free` / `unresolved_no_legal_pdf`.** All providers returned nothing. Most
often the corpus really is closed access — IEEE conference papers and Springer chapters
usually have no free copy anywhere. Check `provider_attempts` on a few results: each provider
records *why* it returned nothing, which distinguishes "Unpaywall has never heard of this DOI"
from "Unpaywall is switched off because no email is configured".

**Every paper takes about the same suspiciously long time.** That is the signature of a host
you cannot reach, since a blocked host costs a full `connect_timeout_seconds` rather than
failing fast. `broad_search` is the usual culprit: eight DuckDuckGo queries per paper, and if
DuckDuckGo is blocked on your network that is eight timeouts per paper for nothing. Drop it
from `source_priority`.

**Papers are recorded as blocked, from a specific publisher.** Check
`host_gate.blocked_hosts()` or the `host_refusals.json` file. If the reason mentions a bot
challenge, no amount of waiting or retrying will help — see [Bot
walls](#bot-walls-and-why-another-machine-does-not-help). If it mentions a rate-shaped
refusal, the cool-off will clear it.

**The PDF path in the result does not exist.** `output.root_dir` defaults to the relative path
`data`, so an unconfigured run writes to `<cwd>/data/pdfs/` and returns a path relative to
whatever the working directory was. Set it to an absolute path.

**Keys are not being picked up.** The `.env` must be in the directory you run from (both
loaders also search upwards). Shell environment variables win over `.env` values. Names must
match exactly — see [Keys](#keys-and-what-each-one-buys).

**`config.json` not found.** It is looked for next to `main.py`. Pass `--config
/absolute/path/config.json` when running from elsewhere.

**Import errors after installing.** Python 3.10 or newer, and `pip install -e .` in the same
environment you run from.

---

## Using this from SEER

SEER is the systematic-review application this library was built for.
It uses both pipelines: `paper_metadata` produces the corpus SEER ingests, and
`paper_downloader` fetches the PDFs that SEER's full-text extraction then reads.

The coupling is deliberately narrow. SEER imports `paper_data` and nothing below it, and this
library knows nothing about SEER at all.

### Metadata: the ingest bundle

Every metadata run writes a **SEER ingest bundle** — a self-describing, schema-versioned
directory SEER imports directly. The frozen interface specification is `00_CONTRACT.md` in
this repository.

```
<bundle_dir>/
    manifest.json      # the run snapshot
    papers.json        # a flat array of paper records
```

For a keyword run it lands at `<base_dir>/runs/<run_id>/seer_ingest/`.

`manifest.json` carries `schema_version` (`"1.0"`; SEER rejects a mismatch), `pipeline`,
`library_version`, `generated_at`, `run_type` (`keyword_search` | `by_id` | `citation_graph`),
an optional `source_label`, `fetch_params`, `counts` (`total_unique_papers`, `per_query`,
`identity_dropped`), and `papers_file`. Keyword runs also carry `queries`; citation runs carry
`seeds`.

Each record in `papers.json` is a standard Semantic Scholar record plus a `_provenance`
object with all six keys always present:

| Field | `keyword_search` | `by_id` | `citation_graph` |
|---|---|---|---|
| `matched_queries` | every query whose results contained this paper, before deduplication | `[]` | `[]` |
| `seed_paper_id` | null | null | the seed's Semantic Scholar id |
| `edge_type` | null | null | `citation` or `reference` |
| `is_influential` | null | null | bool |
| `input_id` | null | the caller's original string | null |
| `fetch_status` | null | `found` / `not_found` / `invalid_id` | null |

Two guarantees the producer makes. **Identity**: every `found` record has at least one of
`paperId`, `externalIds.DOI` or `externalIds.ArXiv`; records failing that are dropped and
counted in `manifest.counts.identity_dropped`. **Intra-run multi-query membership**:
`matched_queries` is complete for this run. Cross-run accumulation — the same paper re-found
by a later run — is SEER's job, not this library's.

Produce one with `export_keyword_bundle()`, `export_by_id_bundle()` or
`export_citation_bundle()`, or the `keyword` / `by-id` / `citation` subcommands of
`python -m paper_metadata.export`.

### PDFs: what SEER configures and what it stores

SEER calls `open_downloader()` once per worker thread and `download_one()` per paper, passing
the paper's stored Semantic Scholar record rather than a bare identifier — which is what lets
it skip the metadata lookup described in [Downloading PDFs](#downloading-pdfs).

It overrides three things in the config it passes:

- **`output.root_dir`** — pinned to an absolute path under SEER's media root, so the returned
  `pdf_path` and SEER's own lookup agree regardless of the process's working directory.
- **`resolution.source_priority`** — filtered by SEER's `PDF_DISABLED_SOURCES`. Its default
  drops `broad_search`, because DuckDuckGo is unreachable from that network.
- **`download.denied_hosts`** — set from SEER's `PDF_DENIED_HOSTS`, four publishers measured
  to refuse this client on the first request from any address.

Keys reach the library the ordinary way: SEER reads them from its own settings and puts them
in the process environment under the names in [Keys](#keys-and-what-each-one-buys).

SEER stores the outcome of each attempt as a status it can act on, and the mapping is the
reason several of the distinctions above exist at all:

| What the library reports | What SEER stores | Retried? |
|---|---|---|
| `record_class` is terminal (`skipped_*`) | `no_full_text` | **Never**, not even by "retry previously failed" |
| `failure_reason: client_challenged` | `client_challenged` | Yes — a re-run after a key is configured takes a different route |
| `oa_asserted_by` non-empty, and a host refused us | `open_access_unfetchable` | Yes |
| A host refused or was deny-listed | `transient_error`, with a detail saying which | Yes |
| `failure_reason: not_yet_available` | `transient_error`, "try again in a few weeks" | Yes |
| `failure_reason: withdrawn` | `no_full_text` | Never |
| 401/403 on the article itself | `blocked_by_publisher` | Only on request |
| Everything else settled | `no_open_access` / `landing_page_no_pdf` | Only on request |

The rule underneath that table: **nothing that is really a fact about our server may be stored
as a verdict on a paper.** A bot wall, a rate ban and a deny-list entry all produce "no PDF",
and recording any of them as `blocked_by_publisher` — which SEER treats as settled — would
turn our own situation into a permanent judgement about someone's article. Conversely,
`no_full_text` is the one status that must never be retried, because the record is not a
document and no run will make it one.

SEER's side of this lives in `papers/enrichment_service.py` and is documented in its
`docs/subsystems/fulltext-pipeline.md`.
