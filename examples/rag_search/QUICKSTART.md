# Quickstart: RAG literature search

This example searches Semantic Scholar for papers on Retrieval-Augmented Generation (RAG) across three focused queries. It is a self-contained starting point — swap in your own queries and topic when ready.

**Estimated run time (fetch only, no recovery):** 2–5 minutes, ~500–2000 papers total.

---

## Prerequisites

- Python 3.10+
- Dependencies installed: `pip install -r ../../requirements.txt`
- Run all commands from the **repository root** (`research_paper_pdf_downloader/`)

**API key (optional but recommended):**

```bash
cp examples/rag_search/.env.example examples/rag_search/.env
# then open .env and paste your Semantic Scholar key
```

Without a key the pipeline works but hits a stricter rate limit, which adds delays between pages.

---

## Step 1 — Preview: see how many results each query will surface

Before fetching anything, check estimated result counts. This makes one lightweight request per query.

```bash
python -m paper_metadata.export preview \
  --search-queries-path examples/rag_search/search_queries.json
```

Example output:
```
Query                             ~Results
-----------------------------------------
1_rag_methods                         843
2_rag_evaluation                      211
3_rag_applications                    672
-----------------------------------------
TOTAL                               1,726
```

Adjust `search_queries.json` until the counts look right before committing to a full run.

---

## Step 2 — Run: fetch papers and deduplicate

```bash
python -m paper_metadata.export keyword \
  --base-dir examples/rag_search/output \
  --search-queries-path examples/rag_search/search_queries.json \
  --label "rag-quickstart" \
  --no-recovery
```

`--no-recovery` skips abstract recovery (faster). Drop that flag to also run API and scrape recovery for missing abstracts.

A timestamped run directory is created:

```
examples/rag_search/output/
  runs/
    20260628-142300_rag-quickstart/
      run.json                  ← status, counts, timestamps
      search_queries.json       ← exact snapshot of the queries used
      raw/                      ← one file per query, raw SS results
      final/                    ← after ID deduplication
      final_title_deduped/      ← after title deduplication
      reports/                  ← CSV logs of all duplicates removed
      seer_ingest/
        manifest.json           ← SEER-compatible bundle
        papers.json
```

---

## Step 3 — Audit: inspect what ran

```bash
python -m paper_metadata.export list-runs \
  --base-dir examples/rag_search/output
```

Example output:
```
Run ID                            Label               Status        Started
---------------------------------------------------------------------------
20260628-142300_rag-quickstart    rag-quickstart      complete      2026-06-28 14:23:00  [1412 papers]
```

The `run.json` inside the run directory records exactly what was searched and what was found:

```bash
cat examples/rag_search/output/runs/*/run.json
```

---

## Programmatic usage

```python
import sys
sys.path.insert(0, ".")  # run from repository root

from paper_data import preview_queries, fetch_metadata, list_runs

# Preview
counts = preview_queries(
    search_queries_path="examples/rag_search/search_queries.json"
)
print(counts)

# Run (no abstract recovery for speed)
fetch_metadata(
    base_dir="examples/rag_search/output",
    search_queries_path="examples/rag_search/search_queries.json",
    label="rag-quickstart",
    api_recovery=False,
    scrape_recovery=False,
)

# Audit
for r in list_runs("examples/rag_search/output"):
    print(r.run_id, r.status, r.counts)
```

---

## Customising the queries

Edit `search_queries.json`. The supported filter keys under `defaults` or per-query:

| Key | What it filters |
|---|---|
| `date_range` | Publication date, e.g. `"2022-01-01:"` (from) or `"2020-01-01:2024-12-31"` (range) |
| `min_citations` | Minimum citation count |
| `publication_types` | Comma-separated SS types: `JournalArticle`, `Conference`, `Study`, `Dataset`, etc. |
| `venue` | Restrict to specific venues, e.g. `"EMNLP,ACL,NAACL"` |
| `fields_of_study` | e.g. `"Computer Science"` |

Run `preview` again after each change to see the impact before fetching.
