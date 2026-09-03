# Automated-Paper-Data-Retriever-System

This is an automated paper data retriever system which has is divided into two pipelines. The first part, **paper_metadata**, fetches academic paper metadata from Semantic Scholar by keyword-driven bulk search, deduplicates by paper ID and title across categories, and recovers missing metadata using multi-sources reterival systems. The second part, **paper_downloader**, resolves and downloads open-access PDFs for given paper identifiers across 11 source providers. Both pipelines are accessible from the command line and as a callable Python library via `paper_data.py`. This repository is under development.

---

## Table of Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [API Keys and Environment Setup](#api-keys-and-environment-setup)
- [Configuration](#configuration)
  - [paper_downloader config.json](#paper_downloader-configjson)
  - [paper_metadata config.json](#paper_metadata-configjson)
- [Paper Metadata Pipeline](#paper-metadata-pipeline)
  - [How It Works](#how-it-works)
  - [search_queries.json](#search_queriesjson)
  - [Workflow: preview → run → audit](#workflow-preview--run--audit)
  - [Running from the CLI](#running-from-the-cli)
  - [Programmatic Usage (paper_metadata)](#programmatic-usage-paper_metadata)
  - [Fetching Metadata by ID](#fetching-metadata-by-id)
  - [Citation and Reference Graph Retrieval](#citation-and-reference-graph-retrieval)
  - [Output Structure (paper_metadata)](#output-structure-paper_metadata)
- [SEER Ingest Bundle](#seer-ingest-bundle)
  - [Bundle Layout](#bundle-layout)
  - [manifest.json Fields](#manifestjson-fields)
  - [papers.json Record Shape](#papersjson-record-shape)
  - [_provenance Rules](#_provenance-rules)
  - [Producing a Bundle (CLI)](#producing-a-bundle-cli)
  - [Producing a Bundle (Python)](#producing-a-bundle-python)
- [Input Formats (paper_downloader)](#input-formats-paper_downloader)
- [Running the Download Pipeline](#running-the-download-pipeline)
- [Output Structure (paper_downloader)](#output-structure-paper_downloader)
- [Source Providers](#source-providers)
- [Resume and Idempotency](#resume-and-idempotency)
- [Programmatic Usage (paper_downloader)](#programmatic-usage-paper_downloader)
- [Troubleshooting](#troubleshooting)

---

## Requirements

- Python 3.10 or higher

---

## Installation

Clone the repository and install the dependencies:

```bash
git clone https://github.com/Panzer3232/research_paper_pdf_downloader.git
cd research_paper_pdf_downloader
pip install -r requirements.txt
```

No other system dependencies are required. All output is written to a local `data/` directory that is created automatically on first run.

---

## API Keys and Environment Setup

API keys are read from a `.env` file in the project root directory.

### Using the pipelines as a library from your own project folder

This is the recommended setup for teams using `paper_data` as a library. You have your own project folder, cloned the repository separately, and call the functions from your own code. You do not need to create `.env` files inside the cloned repository at all.

**Create a single `.env` file in your own project folder** — the folder where you run your script from:

```
/home/user/myproject/
├── my_script.py
└── .env                  ← place your .env here
```

That single `.env` file covers both pipelines. Use this format:

```
SEMANTIC_SCHOLAR_API_KEY=
OPENALEX_API_KEY=
UNPAYWALL_EMAIL=
CORE_API_KEY=
CROSSREF_EMAIL=
```

**Always run your script from the folder that contains your `.env`:**

```bash
cd /home/user/myproject
python my_script.py
```

Both loaders fall back to searching upward from the current working directory when no `.env` is found next to the pipeline's own `config.json`. As long as you run from your project folder, both pipelines will find your `.env` there automatically.

---

## Configuration

### paper_downloader config.json

The `config.json` file controls all pipeline behaviour.

**Fields worth knowing about:**

| Field | What it controls |
|---|---|
| `resolution.source_priority` | Which source providers run, and in what order. This is an **enable list**: a provider whose name is not here is never constructed, so it costs nothing. Names with no implementation are skipped with a warning. |
| `resolution.stop_when_confident` | Stop querying further providers once one has returned a direct PDF link on a trusted host scoring at or above `stop_confidence_threshold`. With `prefer_publisher_version` on, only a publisher-version candidate can end the search, so preprint-only papers still consult every provider. Default on. |
| `download.max_retries` / `retry_backoff_seconds` | Retries per candidate URL, with exponential backoff. Applied only to failures that could go the other way next time — connection errors, timeouts, 429, 5xx. A 401/403/404 is an answer, not a glitch, and is never retried. |
| `download.landing_page_fallback` | When a candidate URL serves HTML instead of a PDF, parse that page for the real PDF link (`citation_pdf_url` meta tag, OJS download link, same-host `.pdf` anchor) and try it once. Most gold-OA DOIs point at an article page rather than a file, so this is the difference between "no PDF" and the PDF. Default on. |
| `download.landing_page_max_bytes` | How much of such a page to read back before scanning it. |

**A note on speed.** `broad_search` queries DuckDuckGo once per trusted domain. If DuckDuckGo is unreachable from your network — some institutions block or sinkhole it — each of those queries costs a full `connect_timeout_seconds`, which can dominate the runtime of every paper. Drop `broad_search` from `source_priority` if so.

---

### paper_metadata config.json

Located at `paper_metadata/config.json`. Controls metadata fetch, deduplication, and abstract recovery behaviour.

**Important fields:**

| Field | What it controls |
|---|---|
| `output.base_dir` | Root directory where all `runs/` subdirectories are created. **Set this to your project folder** |
| `search_queries_path` | Absolute path to `search_queries.json`. **Set this to your queries file location** |
| `semantic_scholar.fields` | Comma-separated list of SS metadata fields returned per paper (returned columns, not search filters — stays in config) |

To add new keyword queries or change search terms, edit `search_queries.json` directly; no code changes needed.

---

## Paper Metadata Pipeline

### How It Works

The pipeline runs five stages in sequence:

**Stage 1 — Semantic Scholar bulk fetch.** For each query defined in `search_queries.json`, the pipeline runs a paginated bulk query against the Semantic Scholar API. Each query fetches up to 1000 papers per request and follows pagination tokens until all results are retrieved. Results are saved to `runs/<run_id>/raw/`.

**Stage 2 — ID-based deduplication.** Within each query, papers sharing the same Semantic Scholar `paperId` are collapsed (intra-category dedup). Then, across all queries in priority order, any paper that already appeared in a higher-priority query is removed from lower-priority ones (inter-category dedup). Each paper ends up in exactly one query bucket. Results are saved to `runs/<run_id>/final/`.

**Stage 3 — Title-based deduplication.** The same paper can exist under two different `paperId` values (preprint + published version). Title-based dedup catches these: titles are normalised (lowercased, punctuation stripped, version suffixes removed) and compared. The version with the higher citation count is kept. Duplicate reports are saved as CSV files in `runs/<run_id>/reports/`. Results are saved to `runs/<run_id>/final_title_deduped/`.

**Stage 4 — API-based abstract recovery.** For papers with missing abstracts, the pipeline tries a chain of API sources: ArXiv (by ID and title), OpenAlex (by DOI and title), PubMed, ACL Anthology, EuropePMC (by DOI, PMID, and title), Crossref, CORE, and Semantic Scholar. Each source applies title similarity verification. Results are saved to `runs/<run_id>/final_recovered_abstract/`.

**Stage 5 — Scrape-based abstract recovery.** For papers still missing an abstract, the pipeline scrapes the publisher webpage via the paper's DOI. Publisher-specific parsers cover Springer, Nature, IEEE, Elsevier, Wiley, Taylor & Francis, Oxford UP, Cambridge UP, Frontiers, MDPI, ACM, PLOS, AAAI, and IJCAI, with a generic JSON-LD fallback. Results are saved to `runs/<run_id>/publisher_scraped/`.

---

### search_queries.json

This file defines the keyword queries used to fetch papers from Semantic Scholar. It has two top-level keys:

- **`defaults`** — search parameters applied to every query unless overridden.
- **`queries`** — a map of query IDs to query entries. Each entry must have a `"query"` string and can override any default.

Query IDs become the filenames for all output JSON files throughout the pipeline. The order of IDs determines inter-query deduplication priority: papers matching multiple queries are assigned to the first matching one.

The Semantic Scholar bulk search supports Boolean operators `|` (OR), `+` (AND), and quoted phrases.

**Supported parameter keys** (in `defaults` or per-query):

| Key | SS API param | Example |
|---|---|---|
| `date_range` | `publicationDateOrYear` | `"2020-01-01:"` |
| `min_citations` | `minCitationCount` | `5` |
| `publication_types` | `publicationTypes` | `"JournalArticle,Conference"` |
| `venue` | `venue` | `"NeurIPS,ICML"` |
| `fields_of_study` | `fieldsOfStudy` | `"Computer Science"` |
| `open_access_pdf` | `openAccessPdf` | `true` |
| `year` | `year` | `"2022-2024"` |

**Example:**

```json
{
  "defaults": {
    "date_range": "2020-01-01:",
    "min_citations": 0,
    "publication_types": "JournalArticle,Conference,Dataset,Study"
  },
  "queries": {
    "1_xai_llm": {
      "query": "(\"explainable AI\" | XAI) + (LLM | \"large language model\")"
    },
    "2_saes_llm": {
      "query": "(\"sparse autoencoder\" | SAE) + (LLM | transformer)",
      "min_citations": 5
    }
  }
}
```

Each query is an atomic, self-contained search. If you need the same topic with different date ranges or citation thresholds, write two query entries — one for each set of parameters.

---

### Workflow: preview → run → audit

The recommended workflow for systematic reviews follows three steps:

**1. Preview** — check how many results each query will surface before committing to a full fetch:

```bash
python -m paper_metadata.export preview \
  --search-queries-path paper_metadata/search_queries.json
```

Or in Python:
```python
counts = preview_queries(search_queries_path="paper_metadata/search_queries.json")
# {'1_xai_llm': 4821, '2_saes_llm': 312, ...}
```

Preview makes one lightweight API request per query (no paper data is fetched). Use it to tune queries before running the full pipeline.

**2. Run** — execute the full pipeline:

```bash
python -m paper_metadata.export keyword \
  --base-dir /data/myproject \
  --label "xai-sweep-june" \
  --no-recovery
```

Each run creates a timestamped directory under `<base_dir>/runs/`:

```
runs/
  20260628-142300_xai-sweep-june/
    run.json                  ← written at start; updated on completion
    search_queries.json       ← exact snapshot of queries used
    raw/
    final/
    final_title_deduped/
    final_recovered_abstract/
    publisher_scraped/
    reports/
    seer_ingest/
```

`run.json` records the run ID, label, start/end times, status, and per-query paper counts.

**3. Audit** — inspect past runs at any time:

```bash
python -m paper_metadata.export list-runs --base-dir /data/myproject
```

Or in Python:
```python
runs = list_runs("/data/myproject")
for r in runs:
    print(r.run_id, r.status, r.counts)
```

Each run directory is fully self-contained: the `search_queries.json` snapshot inside it records exactly what was searched, and `run.json` records the parameters and counts. This satisfies PRISMA documentation requirements for systematic reviews.

---

### Running from the CLI

All commands are run from the **parent directory** of `paper_metadata/` using Python's `-m` flag.

**Preview query result counts (no data fetched):**

```bash
python -m paper_metadata.export preview \
  --search-queries-path paper_metadata/search_queries.json
```

**Full pipeline:**

```bash
python -m paper_metadata.export keyword \
  --base-dir /path/to/project \
  --label "my-run-label"
```

**Full pipeline with explicit queries file:**

```bash
python -m paper_metadata.export keyword \
  --base-dir /path \
  --search-queries-path /path/to/search_queries.json \
  --label "sweep-v2"
```

**List past runs:**

```bash
python -m paper_metadata.export list-runs --base-dir /path/to/project
```

---

### Programmatic Usage (paper_metadata)

```python
from paper_data import fetch_metadata, preview_queries, list_runs, recover_abstracts

# Preview result counts before fetching
counts = preview_queries(search_queries_path="/path/to/search_queries.json")
# {'1_xai_llm': 4821, '2_saes_llm': 312, ...}

# Full pipeline — creates runs/<timestamp>/ under base_dir
fetch_metadata(
    base_dir="/path",
    search_queries_path="/path/to/search_queries.json",
    label="xai-sweep-june",            # optional, appended to run directory name
)

# Full pipeline with default config
fetch_metadata()

# Fetch and dedup only, no recovery
fetch_metadata(api_recovery=False, scrape_recovery=False)

# List all past runs sorted newest-first
runs = list_runs("/path")
for r in runs:
    print(r.run_id, r.status, r.label)
    print(r.counts)           # {query_id: {"raw": ..., "final": ...}}
    print(r.run_dir)          # Path to the run directory

# Run recovery stages on already-fetched data in a prior run's directory
recover_abstracts(
    "/path/runs/20260628-142300_xai-sweep-june/final_title_deduped"
)

# API recovery only
recover_abstracts(
    "/path/runs/.../final_title_deduped",
    scrape_recovery=False,
)

# Scrape recovery only on files that already went through API recovery
recover_abstracts(
    "/path/runs/.../final_recovered_abstract",
    api_recovery=False,
)
```

---

### Fetching Metadata by ID

Use `fetch_papers_by_id` to retrieve full paper metadata for one or more known identifiers without running the keyword search pipeline. Accepts Semantic Scholar paper IDs, DOIs, ArXiv IDs, or any explicitly prefixed identifier (`ARXIV:`, `DOI:`, `ACL:`, `MAG:`, `PMID:`, `PMCID:`, `CorpusId:`). Returns a list of paper dicts in memory — no files are written to disk.

```python
from paper_data import fetch_papers_by_id

# Single paper — any supported ID type
papers = fetch_papers_by_id("2410.20513")
papers = fetch_papers_by_id("10.1016/j.websem.2024.100822")
papers = fetch_papers_by_id("649def34f8be52c8b66281af98ae884c09aef38b")

# Batch — mixed ID types in one call
papers = fetch_papers_by_id([
    "2410.20513",
    "DOI:10.1145/1234567.1234568",
    "CorpusId:215416146",
])

# With abstract recovery for papers missing one after the SS fetch
papers = fetch_papers_by_id(ids, api_recovery=True, scrape_recovery=True)

# Iterate results
for p in papers:
    if p["_fetch_status"] == "found":
        print(p["title"], p.get("abstract", "—"))
    else:
        print(p["_input_id"], "→", p["_fetch_status"])
```

Each result carries `_input_id` (the original identifier supplied) and `_fetch_status` (`found`, `not_found`, or `invalid_id`). Abstract recovery does not run automatically; pass `api_recovery=True` or `scrape_recovery=True` to opt in. Bare numeric IDs are rejected — prefix them explicitly (`CorpusId:`, `PMID:`, or `MAG:`).

---

### Citation and Reference Graph Retrieval

`fetch_citations_and_references` fetches citation and reference edges for one or more papers via two independent Semantic Scholar endpoints: `/paper/{id}/citations` (papers that cite the target) and `/paper/{id}/references` (papers cited by the target). Both are called by default and can be toggled independently. Returns one `PaperGraphResult` per input identifier.

#### Usage

```python
from paper_data import fetch_citations_and_references, CitationGraphOptions

# Both endpoints, all defaults
results = fetch_citations_and_references("2106.15928")

# Batch — citations only, influential papers, capped at 200, saved to disk
results = fetch_citations_and_references(
    [
        "1509b5dd76b251d61cd03f0bc26521da50edcf37",
        "1a1e99514d8d175459f7c61cfd0c394b46e63359",
    ],
    references=False,
    influential_only=True,
    max_results=200,
    save_dir="/path/to/output",
)

# Fine-grained control — different options per endpoint
results = fetch_citations_and_references(
    paper_batch,
    citation_options=CitationGraphOptions(influential_only=True, max_results=200),
    reference_options=CitationGraphOptions(influential_only=False),
    save_dir="/path/to/output",
)

# Iterate results
for r in results:
    if r.error:
        print(r.input_id, "→ failed:", r.error)
    else:
        print(r.input_id, "→", len(r.citations), "citations,", len(r.references), "references")
```

Shorthand kwargs (`influential_only`, `max_results`, `fields`, `publication_date_filter`) apply identically to both endpoints. When `citation_options` or `reference_options` is provided for an endpoint, all shorthand kwargs are ignored for that endpoint — mixing both raises `ValueError`.

#### Parameters

| Parameter | Default | Description |
|---|---|---|
| `citations` | `True` | Call the `/citations` endpoint |
| `references` | `True` | Call the `/references` endpoint |
| `influential_only` | `False` | Filter to edges where `isInfluential=True` (post-fetch in Python; all pages are still fetched) |
| `max_results` | `None` | Cap on edges fetched per endpoint per paper; `None` fetches all up to the API ceiling (~9,999) |
| `fields` | `None` | Comma-separated SS fields per returned paper (e.g. `"paperId,title,year"`); falls back to `config.json` default |
| `publication_date_filter` | `None` | Date range for **citations only**; ignored for references. Format: `"YYYY-MM-DD:YYYY-MM-DD"`, open-ended (`"2020-01-01:"`) accepted |
| `citation_options` | `None` | `CitationGraphOptions` for fine-grained citations control; overrides all shorthand kwargs for citations |
| `reference_options` | `None` | `CitationGraphOptions` for fine-grained references control; overrides all shorthand kwargs for references |
| `save_dir` | `None` | Directory to write JSON output; created automatically if absent |

`influential_only` relies on Semantic Scholar's ML model, which identifies citations where the cited work had significant impact on the citing paper based on citation count and surrounding context. See [Valenzuela et al., 2015](https://www.semanticscholar.org/paper/Identifying-Meaningful-Citations-Valenzuela-Ha/1c7be3fc28296a97607d426f9168ad4836407e4b) for the methodology. Note that `citations_fetched` / `references_fetched` on the result always reflect the raw pre-filter count.

#### Output structure (when save_dir is set)

```
save_dir/
  citations/<paper_id>.json
  references/<paper_id>.json
  fetch_summary.json
```

Each per-paper file records `fetched` (raw count), `returned` (after filtering), `truncated`, and the `papers` list. `fetch_summary.json` covers every paper with per-endpoint `status` values: `success`, `empty`, `truncated`, `failed`, or `not_requested`.

#### PaperGraphResult fields

| Field | Type | Description |
|---|---|---|
| `input_id` | `str` | Original identifier supplied by the caller |
| `paper_id` | `str \| None` | Normalised identifier sent to the API; `None` on error |
| `citations` | `list[dict]` | Citing papers (empty if not requested or on error) |
| `references` | `list[dict]` | Cited papers (empty if not requested or on error) |
| `citations_fetched` / `references_fetched` | `int` | Raw edge count before `influential_only` filtering |
| `citations_truncated` / `references_truncated` | `bool` | `True` if the API ceiling (~9,999) was hit |
| `error` | `str \| None` | Error message on lookup failure; `None` on success |

#### Identifier formats and API limits

Accepted formats are the same as `fetch_papers_by_id`: 40-char hex SS IDs, DOIs (`10.XXXX/...`), ArXiv IDs (`2106.15928`), URLs, and explicitly prefixed identifiers (`DOI:`, `ARXIV:`, `CorpusId:`, `PMID:`, `PMCID:`, `MAG:`, `ACL:`). Bare numeric strings are rejected — use an explicit prefix (e.g. `"CorpusId:12345678"`).

The API silently truncates at ~9,999 edges per endpoint; `citations_truncated` / `references_truncated` signal this. Set `SEMANTIC_SCHOLAR_API_KEY` in `.env` to raise rate limits. The inter-paper delay in batch calls is controlled by `request_delay` in the `citation_graph` section of `paper_metadata/config.json`.

---

### Output Structure (paper_metadata)

Each pipeline run creates a timestamped directory under `<base_dir>/runs/`:

```
<base_dir>/
  runs/
    20260628-142300_xai-sweep-june/
      run.json                      # run metadata: status, timestamps, per-query counts
      search_queries.json           # snapshot of the queries used for this run
      raw/
        1_xai_llm.json              # raw SS fetch
        2_mi_llm.json
        ...
      final/
        1_xai_llm.json              # after ID-based deduplication
        ...
      final_title_deduped/
        1_xai_llm.json              # after title-based deduplication
        ...
      final_recovered_abstract/
        1_xai_llm.json              # after API-based abstract recovery
        ...
      publisher_scraped/
        1_xai_llm.json              # after scrape-based abstract recovery
        ...
      reports/
        acquisition_stats.json
        title_dedup_stats.json
        intra_title_duplicates.csv
        inter_title_duplicates.csv
      seer_ingest/
        manifest.json
        papers.json
    20260615-093000/                # earlier run, unaffected
      ...
```

Each stage reads from the previous stage's directory and writes to its own. The `run.json` file is written at the start of each run (status: `running`) and updated on completion (status: `complete`).

**`run.json` schema:**

```json
{
  "run_id": "20260628-142300_xai-sweep-june",
  "label": "xai-sweep-june",
  "started_at": "2026-06-28T14:23:00",
  "completed_at": "2026-06-28T15:47:00",
  "status": "complete",
  "search_queries_file": "/original/path/search_queries.json",
  "stages": { "api_recovery": true, "scrape_recovery": false },
  "counts": {
    "1_xai_llm": { "raw": 4821, "final": 2103 },
    "2_mi_llm":  { "raw": 1240, "final": 891 }
  }
}
```

---

## SEER Ingest Bundle

Every pipeline run automatically produces a **SEER ingest bundle** — a self-describing,
schema-versioned directory that the SEER Django application can import directly.  The
bundle is the single coupling point between this library (the producer) and SEER (the
consumer); neither side reaches into the other's internal data structures.

The frozen interface specification lives in `00_CONTRACT.md` in this repository.

### Bundle Layout

```
<bundle_dir>/
    manifest.json      # run-level metadata (the "run snapshot")
    papers.json        # flat JSON array of paper records
```

The default location for a keyword-search run is:

```
<base_dir>/runs/<run_id>/seer_ingest/
```

### manifest.json Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | string | always | `"1.0"` — SEER rejects mismatches |
| `pipeline` | string | always | constant `"paper_metadata"` |
| `library_version` | string | always | git SHA or package version |
| `generated_at` | string | always | ISO-8601 UTC timestamp |
| `run_type` | string | always | `keyword_search` \| `by_id` \| `citation_graph` |
| `source_label` | string\|null | optional | human label passed via `--label` |
| `fetch_params` | object | always | date filters + citation thresholds |
| `queries` | object | `keyword_search` | category key → SS query string |
| `seeds` | array | `citation_graph` | list of `{seed_paper_id, edges}` |
| `counts` | object | always | `total_unique_papers`, `per_query`, `identity_dropped` |
| `papers_file` | string | always | `"papers.json"` |

`fetch_params` shape for `keyword_search`: per-query SS API params keyed by query ID:

```json
{
  "1_xai_llm": { "publicationDateOrYear": "2020-01-01:", "publicationTypes": "JournalArticle,Conference" },
  "2_saes_llm": { "publicationDateOrYear": "2020-01-01:", "minCitationCount": 5 }
}
```

### papers.json Record Shape

Each element is a standard Semantic Scholar metadata record plus a `_provenance` object:

```jsonc
{
  "paperId": "649def34f8be52c8b66281af98ae884c09aef38b",
  "externalIds": { "DOI": "10.1016/...", "ArXiv": "2410.20513" },
  "title": "…",
  "abstract": "…",
  "authors": [{ "authorId": "…", "name": "…" }],
  "year": 2023,
  "publicationDate": "2023-05-04",
  "venue": "…",
  "citationCount": 42,
  "isOpenAccess": true,
  "openAccessPdf": { "url": "https://…pdf" },

  "_provenance": {
    "matched_queries": ["1_xai_llm", "3_activation_weight_llm"],
    "seed_paper_id": null,
    "edge_type": null,
    "is_influential": null,
    "input_id": null,
    "fetch_status": null
  }
}
```

`_provenance` per run type:

| Field | `keyword_search` | `by_id` | `citation_graph` |
|---|---|---|---|
| `matched_queries` | non-empty list of query keys | `[]` | `[]` |
| `seed_paper_id` | null | null | SS paper ID of the seed |
| `edge_type` | null | null | `"citation"` or `"reference"` |
| `is_influential` | null | null | bool |
| `input_id` | null | original caller string | null |
| `fetch_status` | null | `"found"` \| `"not_found"` \| `"invalid_id"` | null |

### _provenance Rules

1. **Always present.** Every record has `_provenance` with all six keys (unused ones are `null` / `[]`).
2. **`keyword_search`**: `matched_queries` is non-empty. Every key is present in `manifest.queries`. It includes the category that owns the paper *and* every other query whose result set contained the same paper before deduplication — intra-run multi-query membership is fully captured here.
3. **`citation_graph`**: `seed_paper_id` non-null, `edge_type` ∈ `{"citation","reference"}`, `is_influential` is a bool. A paper appearing under multiple seeds yields multiple records; SEER deduplicates and keeps both edges.
4. **`by_id`**: `input_id` non-null, `fetch_status` ∈ `{"found","not_found","invalid_id"}`. Records with `fetch_status != "found"` are included so SEER can log them.
5. **Identity guarantee.** Every `found` record has at least one of `paperId`, `externalIds.DOI`, or `externalIds.ArXiv`. Records that fail this check are dropped and counted in `manifest.counts.identity_dropped`.

**Division of labour:** the producer (this library) guarantees intra-run multi-query
membership in `matched_queries`.  Cross-run accumulation (the same paper re-found by a
later run) and `was_new_at_ingest` tracking are SEER's responsibility.

### Producing a Bundle (CLI)

```bash
# Preview result counts — one lightweight request per query, no data fetched
python -m paper_metadata.export preview \
  --search-queries-path paper_metadata/search_queries.json

# Keyword sweep — bundle lands in <base_dir>/runs/<run_id>/seer_ingest/
python -m paper_metadata.export keyword \
  --base-dir /data/myproject \
  --label "xai-sweep-2026-07"

# Skip abstract recovery for a faster test run
python -m paper_metadata.export keyword \
  --base-dir /data/myproject \
  --no-recovery

# List past runs
python -m paper_metadata.export list-runs --base-dir /data/myproject

# Explicit paper ID list
python -m paper_metadata.export by-id \
  --ids paper_ids.json \
  --bundle-dir /data/bundles/manual-batch-1 \
  --label "manual seed set"

# Snowball from seed papers (fetch both citations and references)
python -m paper_metadata.export citation \
  --ids seeds.json \
  --bundle-dir /data/bundles/snowball-r1 \
  --citations --references \
  --label "snowball round 1"
```

`--ids` accepts either a JSON file containing a list of ID strings, or a
comma-separated list of IDs directly on the command line.

When neither `--citations` nor `--references` is specified for the `citation`
subcommand, both are fetched by default.

### Producing a Bundle (Python)

```python
from paper_data import export_keyword_bundle, export_by_id_bundle, export_citation_bundle

# Keyword sweep — run lands in <base_dir>/runs/<run_id>/seer_ingest/
bundle_dir = export_keyword_bundle(
    base_dir="/data/myproject",
    source_label="XAI/LLM sweep 2026-07",
    label="xai-sweep-2026-07",         # appended to run directory name
)

# Explicit IDs
bundle_dir = export_by_id_bundle(
    ["2106.15928", "DOI:10.18653/v1/N18-3011"],
    bundle_dir="/data/bundles/manual-1",
    source_label="manual seed set",
)

# Citation graph
bundle_dir = export_citation_bundle(
    ["649def34f8be52c8b66281af98ae884c09aef38b"],
    bundle_dir="/data/bundles/snowball-r1",
    citations=True,
    references=True,
    source_label="snowball round 1",
)
```

All three functions re-run the relevant fetch (they do not read from existing output
files). If you have already run `fetch_metadata()`, the keyword bundle will have been
written automatically to `<base_dir>/runs/<run_id>/seer_ingest/`.

---

## Input Formats (paper_downloader)

The pipeline accepts several input formats, all passed via the `--input` argument.

**A JSON file containing a list of Semantic Scholar metadata records**

This is the recommended format if you are working with data exported from Semantic Scholar. You can just input papers metadata json file, based on the semantic scholar paperID it will download the papers and in the end new json file is given with download stats (file path, downloaded status etc).

**A JSON file containing a list of identifier strings**

A plain list of identifiers. Each string can be a Semantic Scholar paper ID. Although it works with arxivID but paperID is safe.

**A single identifier string passed directly**

```bash
python main.py --input "004e5d24c1e8511519fc081b6d723c55651f80b9"
python main.py --input "10.1016/j.websem.2024.100822"
python main.py --input "2410.20513"
```

---

## Running the Download Pipeline

Basic usage (config file is default):

```bash
python main.py --input your_papers.json
```

With an explicit config file (if custom config file is used):

```bash
python main.py --input your_papers.json --config config.json
```

With enriched output — writes a copy of your input JSON with `pdf_path`, `download_status`, and `downloaded` fields added to each record:

```bash
python main.py --input your_papers.json --output your_papers_enriched.json
```

If `--output` is not provided, the enriched file is written automatically as `your_papers_enriched.json` in the same directory as the input file.

With a custom stats output directory and a label to identify this run:

```bash
python main.py --input your_papers.json --stats-dir /path/to/stats --run-label batch_01
```

With logging to a file: set `"log_file": "pipeline.log"` in the `logging` section of `config.json`.

---

## Output Structure (paper_downloader)

After a run, the following directory structure is created under the configured `root_dir` (default `data/`):

```
data/
  pdfs/
    arxiv__2410.20513.pdf
    doi__10.1016_j.websem.2024.100822.pdf
    ...
  metadata/
    arxiv__2410.20513.json
    ...
  manifests/
    arxiv__2410.20513.json
    ...
  download_stats/
    download_stats_20260328T230509Z_full.json
    download_stats_20260328T230509Z_short.json
    download_stats_20260328T230509Z_short.csv
```

**PDFs** are named by the paper key derived from the best available identifier. The key format is `doi__...`, `arxiv__...`, `ss__...`, or `corpus__...`.

**Metadata** files are JSON snapshots of the paper record as it was known at the time of processing, including all recovered identifiers.

**Manifests** are per-paper JSON files that record the full processing history: every pipeline stage, its status, timestamps, retry count, and the source that was selected and downloaded. If a run is interrupted, manifests allow the pipeline to resume correctly on the next run without re-downloading files that already exist.

**Stats files** are written after every run. Three files are produced per run with a UTC timestamp in the filename so no run overwrites a previous one:

- `_full.json` — complete result for every paper including all provider attempts, download attempts, selected source, and error details.
- `_short.json` — compact version with one row per paper: paper ID, title, downloaded (true/false), status, and pdf_path.
- `_short.csv` — same compact data in CSV format for spreadsheet use.

**Enriched input file** — if `--output` is specified or auto-derived, a copy of your input JSON is written with three fields added to each paper record:

- `pdf_path` — absolute path to the downloaded PDF, or null if the download failed.
- `download_status` — one of `downloaded`, `already_exists`, `failed_unresolved_no_legal_pdf`, `failed_download_failed_all_candidates`, or similar.
- `downloaded` — boolean, true if a PDF is available on disk.

---

## Source Providers

The pipeline queries up to 11 open-access source providers in priority order. Providers are tried sequentially. All candidates from all providers are collected, scored, and ranked before the best one is selected for download.

| Provider | What it does | API key required |
|---|---|---|
| metadata_open_access | Reads the `openAccessPdf` URL directly from Semantic Scholar metadata | No |
| arxiv | Constructs the PDF URL directly from the ArXiv ID | No |
| acl | Constructs the PDF URL from the ACL Anthology ID or ACL DOI | No |
| cvf | Constructs the PDF URL from the CVF (CVPR/ICCV/ECCV) paper title | No |
| openalex | Looks up open-access locations via the OpenAlex API | Optional |
| unpaywall | Looks up open-access locations via the Unpaywall API | Email required |
| europepmc | Searches EuropePMC for life science papers with full text | No |
| crossref | Extracts PDF links from Crossref work metadata | Optional (email) |
| core | Searches CORE for repository copies | Key required |
| zenodo | Searches Zenodo for deposited copies | No |
| doaj | Searches the Directory of Open Access Journals | No |
| broad_search | Falls back to DuckDuckGo site-scoped search as a last resort | No |

### Scoring System

The scoring system prefers publisher versions over accepted manuscripts over preprints. Within each version type, candidates from trusted domains are ranked above unknown domains. A paper is considered downloaded if any candidate succeeds. If all candidates fail, the paper is marked as `failed_unresolved_no_legal_pdf` and appears in the failed count in the stats summary.

When multiple providers return PDF candidates for the same paper, the pipeline ranks them using a two-layer system before attempting any download. The first layer is a hard categorical sort: candidates are ordered by whether they are a publisher version, whether the URL is a direct `.pdf` link, and whether the hosting server is a known publisher domain — in that priority order, evaluated as a tuple so a confirmed publisher version always outranks a preprint regardless of score. The second layer is a continuous quality score built from six independent additive signals: a direct PDF link adds `+0.20`, a domain matching the `trusted_domains` config list adds `+0.20`, a publisher version type adds `+0.30` (or `+0.15` if `prefer_publisher_version` is false) while an accepted version adds `+0.20`, a preprint adds `+0.10` if allowed or `-1.00` if `allow_preprints` is false effectively eliminating it, a publisher host type adds `+0.10` and a repository host `+0.05`, a title similarity score at or above `title_similarity_threshold` adds `+0.10` while a score below threshold adds `-0.20`, and finally the provider's own base confidence — ranging from `0.62` for BroadSearch up to `0.97` for ACL Anthology — contributes at most `+0.10` (scaled by a factor of `0.10`) so that the pipeline's own structural signals always outweigh any single provider's self-reported certainty. Before ranking, duplicate URLs across providers are collapsed keeping only the higher-scored entry. The top-ranked candidate is attempted first; if it fails to download, the pipeline falls through the ranked list automatically. Every candidate's full score breakdown is persisted in the paper's manifest JSON under `stats.resolution.all_candidates` for inspection.

---

## Resume and Idempotency

The pipeline is safe to re-run on the same input. On each run:

- Papers whose PDF already exists on disk and whose manifest shows a completed download stage are skipped. The log will say `pdf already exists, skipping download`.
- Papers that previously failed are retried from the failed stage.
- Stats files use timestamps in their names so each run produces new files without overwriting previous results.

To force a full re-download of everything, delete the `data/` directory before running.

---

## Programmatic Usage (paper_downloader)

The pipeline can be used as a callable library without the CLI. This is useful when integrating the downloader into a larger pipeline or calling it from another script.

### Installation

From the project root, install in editable mode once:

```bash
pip install -e .
```

After this, `paper_data.py` is importable from any folder without path manipulation.

### Basic Usage

```python
from paper_data import download

results = download("649def34f8be52c8b66281af98ae884c09aef38b")

results = download([
    "649def34f8be52c8b66281af98ae884c09aef38b",
])

for r in results:
    if r.downloaded:
        print(r.pdf_path)
    else:
        print(r.status, r.error)
```

### Walking a list one paper at a time

`download()` builds a fresh orchestrator per call — twelve provider HTTP sessions, a Semantic Scholar client, and a re-read of `config.json`. That is wasted work if you are looping over papers to record progress after each one. Open the downloader once instead:

```python
from paper_data import open_downloader

with open_downloader(config=cfg) as dl:
    for paper_id in ids:
        result = dl.download_one(paper_id)
        record(result)          # your own progress reporting
```

The handle is **not** thread-safe: the providers hold `requests.Session` objects, which must not be shared across threads. For parallelism, open one handle per thread — the sessions are ones that thread needed anyway.

---

## Troubleshooting

**The pipeline reports a paper as `failed_unresolved_no_legal_pdf`**

This means all 11 providers returned no downloadable PDF for that paper. Common reasons: the paper is closed access with no preprint (IEEE, ACM, Elsevier without OA), or it is too recent to have been indexed by repositories. The pipeline does not attempt to bypass paywalls and will not download content that is not legally open access.

**The pipeline is slow**

The pipeline processes papers sequentially, and each paper queries external APIs in sequence, so runtime is dominated by network latency and by any rate-limiting backoff.

Check the obvious culprit first: **is every paper taking about the same suspiciously long time?** That is the signature of a provider whose host you cannot reach, since a blocked host costs a full `connect_timeout_seconds` rather than failing fast. `broad_search` is the usual one — it makes eight DuckDuckGo queries per paper, and if DuckDuckGo is blocked on your network that is eight connect timeouts, every paper, contributing nothing. Drop it from `resolution.source_priority`.

Otherwise: provide API keys (Semantic Scholar and CORE especially) to reduce rate-limiting delays, keep `resolution.stop_when_confident` on, and note that papers with ArXiv IDs resolve in one or two provider calls and are fast.

**Import errors after installation**

Ensure you are running Python 3.10 or higher and that you installed dependencies with `pip install -r requirements.txt` in the same environment. Check that your working directory is the repository root when running `python main.py`, so that the `app/` package is on the Python path.

**Config file not found**

The pipeline looks for `config.json` next to `main.py` by default. If you run `main.py` from a different directory, pass the config path explicitly with `--config /path/to/config.json`.

**API keys not being picked up**

Ensure your `.env` file is in the same directory as `main.py` and that the variable names exactly match those listed in the API Keys section above. Environment variables set in the shell take precedence over `.env` file values.

**paper_metadata: search_queries.json or base_dir not found**

Set the `search_queries_path` field or `base_dir` in `config.json` to the absolute path of your `search_queries.json` file, or pass both explicitly on the command line:

```bash
python -m paper_metadata.main \
  --base-dir /your/output/path \
  --search-queries-path /path/to/search_queries.json
```