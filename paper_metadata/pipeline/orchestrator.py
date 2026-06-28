from __future__ import annotations

import json
import logging
import shutil
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ..acquisition.semantic_scholar import fetch_all_categories, load_search_queries
from ..config.models import MetadataConfig, RunConfig, SearchQuery
from ..deduplication.id_dedup import deduplicate_inter as id_dedup_inter
from ..deduplication.title_dedup import deduplicate_inter_title, deduplicate_intra_title
from ..recovery.api_recovery import ApiRecoveryProvider
from ..recovery.scrape_recovery import ScrapeRecoveryProvider, _publisher_from_doi
from ..reporting.reporter import (
    print_acquisition_report,
    print_overall_scrape_summary,
    print_recovery_report,
    print_scrape_report,
    print_title_dedup_report,
    save_csv,
    save_stats_json,
)

logger = logging.getLogger(__name__)

_INTRA_CSV_FIELDS = [
    "category", "scope", "normalized_title",
    "kept_paperId", "kept_citations", "kept_title",
    "dropped_paperId", "dropped_citations", "dropped_title",
]
_INTER_CSV_FIELDS = [
    "owner_category", "challenger_category", "scope", "normalized_title",
    "kept_paperId", "kept_citations", "kept_title",
    "dropped_paperId", "dropped_citations", "dropped_title",
    "citation_swap_performed",
]


def _save_json(data: list | dict, filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _load_json(filepath: Path) -> list | dict:
    with open(filepath, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _is_missing_abstract(paper: dict) -> bool:
    abstract = paper.get("abstract")
    if not abstract:
        return True
    return not str(abstract).strip()


class MetadataOrchestrator:
    def __init__(self, config: MetadataConfig, run_config: RunConfig | None = None) -> None:
        self._config = config
        self._run_config = run_config or RunConfig()
        base = Path(config.output.base_dir).resolve()

        rc = self._run_config
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        label_suffix = f"_{rc.label}" if rc.label else ""
        self._run_id = f"{ts}{label_suffix}"
        self._run_root = base / "runs" / self._run_id

        self._dirs = {
            "raw":                      self._run_root / "raw",
            "final":                    self._run_root / "final",
            "final_title_deduped":      self._run_root / "final_title_deduped",
            "final_recovered_abstract": self._run_root / "final_recovered_abstract",
            "publisher_scraped":        self._run_root / "publisher_scraped",
            "reports":                  self._run_root / "reports",
            "seer_ingest":              self._run_root / "seer_ingest",
        }

    def run(self) -> Path | None:
        self._create_dirs()
        rc = self._run_config

        started_at = datetime.now().isoformat()
        run_manifest: dict = {
            "run_id": self._run_id,
            "label": rc.label,
            "started_at": started_at,
            "completed_at": None,
            "status": "running",
            "search_queries_file": str(self._config.search_queries_path),
            "stages": {
                "api_recovery": rc.run_api_recovery,
                "scrape_recovery": rc.run_scrape_recovery,
            },
            "counts": {},
        }
        _save_json(run_manifest, self._run_root / "run.json")

        sq_src = Path(self._config.search_queries_path)
        if sq_src.exists():
            shutil.copy2(sq_src, self._run_root / "search_queries.json")

        if rc.input_dir is not None:
            input_dir = Path(rc.input_dir).resolve()
            if not input_dir.exists():
                raise FileNotFoundError(f"--input-dir does not exist: {input_dir}")

            category_order = self._categories_from_dir(input_dir)
            if not category_order:
                raise ValueError(f"No .json files found in --input-dir: {input_dir}")

            logger.info(
                "Input-dir mode: %d categories found in %s", len(category_order), input_dir
            )

            api_output_dir = self._dirs["final_recovered_abstract"]
            scrape_input_dir = input_dir

            if rc.run_api_recovery:
                logger.info("Running API-based abstract recovery")
                self._run_api_recovery(category_order, input_dir, api_output_dir)
                scrape_input_dir = api_output_dir

            if rc.run_scrape_recovery:
                logger.info("Running scrape-based abstract recovery")
                self._run_scrape_recovery(category_order, scrape_input_dir, self._dirs["publisher_scraped"])

            if not rc.run_api_recovery and not rc.run_scrape_recovery:
                logger.warning(
                    "--input-dir given but no recovery stage requested. "
                    "Use --api-recovery and/or --scrape-recovery."
                )
            run_manifest["completed_at"] = datetime.now().isoformat()
            run_manifest["status"] = "complete"
            _save_json(run_manifest, self._run_root / "run.json")
            return None

        # Full pipeline mode
        search_queries = load_search_queries(self._config.search_queries_path)
        if not search_queries:
            raise ValueError(
                f"search_queries.json has no queries. Add at least one entry under 'queries'. "
                f"(path: {self._config.search_queries_path})"
            )

        category_order = [sq.id for sq in search_queries]
        logger.info("Full pipeline: %d queries", len(category_order))

        # Stage 1 + intra ID dedup
        logger.info("Stage 1: Fetching from Semantic Scholar")
        fetch_results = fetch_all_categories(
            self._config,
            search_queries,
            self._dirs["raw"],
        )

        # Build multi-query membership map before inter-ID dedup discards duplicates.
        membership: dict[str, set[str]] = {}
        for sq in search_queries:
            for paper in fetch_results[sq.id]["papers"]:
                pid = paper.get("paperId")
                if pid:
                    membership.setdefault(pid, set()).add(sq.id)

        # Stage 2: Inter ID dedup
        logger.info("Stage 2: Inter-category ID deduplication")
        intra_deduped = {sq.id: fetch_results[sq.id]["papers"] for sq in search_queries}
        assigned_id = id_dedup_inter(intra_deduped)
        acquisition_stats: dict[str, dict] = {}

        for sq in search_queries:
            raw_count    = fetch_results[sq.id]["raw_count"]
            intra_dupes  = fetch_results[sq.id]["intra_dupes"]
            after_intra  = len(fetch_results[sq.id]["papers"])
            final_papers = assigned_id[sq.id]["papers"]
            inter_removed = assigned_id[sq.id]["inter_removed"]

            _save_json(final_papers, self._dirs["final"] / f"{sq.id}.json")

            acquisition_stats[sq.id] = {
                "raw":           raw_count,
                "intra_dupes":   intra_dupes,
                "after_intra":   after_intra,
                "inter_removed": inter_removed,
                "final_unique":  len(final_papers),
            }

        print_acquisition_report(acquisition_stats, str(self._dirs["final"]))
        save_stats_json(
            {
                "generated_at": datetime.now().isoformat(),
                "queries":      {sq.id: sq.ss_params for sq in search_queries},
                "categories":   acquisition_stats,
            },
            self._dirs["reports"] / "acquisition_stats.json",
        )

        # Stage 3: Title dedup
        logger.info("Stage 3: Title-based deduplication")
        title_input: dict[str, list[dict]] = {
            sq.id: assigned_id[sq.id]["papers"] for sq in search_queries
        }
        intra_title_data: dict[str, list[dict]] = {}
        all_intra_rows: list[dict] = []
        title_stats: dict[str, dict] = {}

        all_intra_merge: dict[str, list[str]] = {}
        for sq in search_queries:
            unique, intra_rows, intra_merge = deduplicate_intra_title(title_input[sq.id], sq.id)
            intra_title_data[sq.id] = unique
            all_intra_rows.extend(intra_rows)
            for kept_pid, dropped_pids in intra_merge.items():
                all_intra_merge.setdefault(kept_pid, []).extend(dropped_pids)
            title_stats[sq.id] = {
                "input":         len(title_input[sq.id]),
                "intra_removed": len(title_input[sq.id]) - len(unique),
                "after_intra":   len(unique),
            }

        assigned_title, inter_rows, inter_dropped, inter_merge = deduplicate_inter_title(intra_title_data)

        for kept_pid, dropped_pids in all_intra_merge.items():
            for dropped_pid in dropped_pids:
                if dropped_pid:
                    membership.setdefault(kept_pid, set()).update(
                        membership.get(dropped_pid, set())
                    )

        for kept_pid, dropped_pids in inter_merge.items():
            for dropped_pid in dropped_pids:
                if dropped_pid:
                    membership.setdefault(kept_pid, set()).update(
                        membership.get(dropped_pid, set())
                    )

        for sq in search_queries:
            papers = assigned_title[sq.id]
            _save_json(papers, self._dirs["final_title_deduped"] / f"{sq.id}.json")
            title_stats[sq.id]["inter_removed"] = title_stats[sq.id]["after_intra"] - len(papers)
            title_stats[sq.id]["final_unique"]  = len(papers)

        total_intra_dropped = sum(s["intra_removed"] for s in title_stats.values())

        save_csv(all_intra_rows, self._dirs["reports"] / "intra_title_duplicates.csv", _INTRA_CSV_FIELDS)
        save_csv(inter_rows,     self._dirs["reports"] / "inter_title_duplicates.csv", _INTER_CSV_FIELDS)
        save_stats_json(
            {
                "generated_at":        datetime.now().isoformat(),
                "total_intra_removed": total_intra_dropped,
                "total_inter_removed": inter_dropped,
                "total_removed":       total_intra_dropped + inter_dropped,
                "categories":          title_stats,
            },
            self._dirs["reports"] / "title_dedup_stats.json",
        )
        print_title_dedup_report(
            title_stats,
            total_intra_dropped,
            inter_dropped,
            str(self._dirs["final_title_deduped"]),
            str(self._dirs["reports"]),
        )

        # Stage 4: API recovery (optional)
        api_output_dir   = self._dirs["final_recovered_abstract"]
        scrape_input_dir = self._dirs["final_title_deduped"]

        if rc.run_api_recovery:
            logger.info("Stage 4: API-based abstract recovery")
            self._run_api_recovery(
                category_order,
                self._dirs["final_title_deduped"],
                api_output_dir,
            )
            scrape_input_dir = api_output_dir

        # Stage 5: Scrape recovery (optional)
        if rc.run_scrape_recovery:
            logger.info("Stage 5: Scrape-based abstract recovery")
            self._run_scrape_recovery(
                category_order,
                scrape_input_dir,
                self._dirs["publisher_scraped"],
            )

        run_manifest["completed_at"] = datetime.now().isoformat()
        run_manifest["status"] = "complete"
        run_manifest["counts"] = {
            sq.id: {
                "raw":   acquisition_stats[sq.id]["raw"],
                "final": title_stats[sq.id].get("final_unique", 0),
            }
            for sq in search_queries
        }
        _save_json(run_manifest, self._run_root / "run.json")

        return self._emit_keyword_bundle(
            search_queries=search_queries,
            membership=membership,
            acquisition_stats=acquisition_stats,
            title_stats=title_stats,
            rc=rc,
        )

    def _emit_keyword_bundle(
        self,
        *,
        search_queries: list[SearchQuery],
        membership: dict[str, set[str]],
        acquisition_stats: dict[str, dict],
        title_stats: dict[str, dict],
        rc,
    ) -> Path:
        from ..export.seer_bundle import get_library_version, write_bundle

        best_dir = (
            self._dirs["publisher_scraped"] if rc.run_scrape_recovery else
            self._dirs["final_recovered_abstract"] if rc.run_api_recovery else
            self._dirs["final_title_deduped"]
        )

        flat_papers: list[dict] = []
        for sq in search_queries:
            cat_file = best_dir / f"{sq.id}.json"
            if not cat_file.exists():
                logger.warning("Bundle: category file not found, skipping: %s", cat_file)
                continue
            for paper in _load_json(cat_file):
                pid = paper.get("paperId")
                matched = sorted(membership.get(pid, {sq.id})) if pid else [sq.id]
                paper["_provenance"] = {
                    "matched_queries": matched,
                    "seed_paper_id":   None,
                    "edge_type":       None,
                    "is_influential":  None,
                    "input_id":        None,
                    "fetch_status":    None,
                }
                flat_papers.append(paper)

        per_query_counts = {
            sq.id: {
                "raw":   acquisition_stats[sq.id]["raw"],
                "final": title_stats[sq.id].get("final_unique", 0),
            }
            for sq in search_queries
        }

        manifest_extra = {
            "queries":      {sq.id: sq.query for sq in search_queries},
            "seeds":        [],
            "fetch_params": {sq.id: sq.ss_params for sq in search_queries},
            "source_label": rc.source_label,
            "counts":       {"per_query": per_query_counts},
        }

        bundle_dir = self._dirs["seer_ingest"]
        write_bundle(
            bundle_dir,
            run_type="keyword_search",
            papers=flat_papers,
            manifest_extra=manifest_extra,
            library_version=get_library_version(),
        )
        logger.info("SEER ingest bundle written to %s", bundle_dir)
        return bundle_dir

    def _run_api_recovery(
        self,
        category_order: list[str],
        input_dir: Path,
        output_dir: Path,
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        provider = ApiRecoveryProvider(
            config=self._config.recovery,
            ss_api_key=self._config.ss_api_key,
            core_api_key=self._config.core_api_key,
        )
        sleep_between = self._config.recovery.api_sleep_between_papers

        for cat in category_order:
            input_path  = input_dir / f"{cat}.json"
            output_path = output_dir / f"{cat}.json"

            if not input_path.exists():
                logger.warning("API recovery: input file not found, skipping: %s", input_path)
                continue

            papers: list[dict] = _load_json(input_path)
            missing = [p for p in papers if _is_missing_abstract(p)]
            total        = len(papers)
            missing_count = len(missing)

            logger.info("[%s] Total: %d | Missing abstracts: %d", cat, total, missing_count)

            if missing_count == 0:
                _save_json(papers, output_path)
                continue

            recovered_count = 0
            source_counts: dict[str, int] = {}
            start_time = time.time()

            for idx, paper in enumerate(missing, start=1):
                paper_start   = time.time()
                result        = provider.recover(paper)
                elapsed_paper = time.time() - paper_start
                elapsed_total = time.time() - start_time
                avg_per_paper = elapsed_total / idx
                eta           = avg_per_paper * (missing_count - idx)

                if result:
                    paper["abstract"] = result.abstract
                    recovered_count  += 1
                    source_counts[result.source] = source_counts.get(result.source, 0) + 1
                    logger.info(
                        "[%s][%d/%d] RECOVERED via %s (%.1fs) | Recovered: %d | ETA: %.0fs | %s",
                        cat, idx, missing_count, result.source,
                        elapsed_paper, recovered_count, eta,
                        paper.get("title", "")[:60],
                    )
                else:
                    logger.info(
                        "[%s][%d/%d] FAILED (%.1fs) | ETA: %.0fs | %s",
                        cat, idx, missing_count,
                        elapsed_paper, eta,
                        paper.get("title", "")[:60],
                    )

                if idx < missing_count:
                    time.sleep(sleep_between)

            total_time = time.time() - start_time
            _save_json(papers, output_path)

            print_recovery_report(
                total=total,
                missing_count=missing_count,
                recovered_count=recovered_count,
                source_counts=source_counts,
                total_time=total_time,
                output_path=str(output_path),
            )

    def _run_scrape_recovery(
        self,
        category_order: list[str],
        input_dir: Path,
        output_dir: Path,
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        scrape_provider = ScrapeRecoveryProvider(config=self._config.recovery)

        all_stats: list[tuple[str, dict]]  = []
        combined_missing:   dict[str, int] = defaultdict(int)
        combined_recovered: dict[str, int] = defaultdict(int)

        for cat in category_order:
            input_path  = input_dir  / f"{cat}.json"
            output_path = output_dir / f"{cat}.json"

            if not input_path.exists():
                logger.warning("Scrape recovery: input file not found, skipping: %s", input_path)
                continue

            papers: list[dict] = _load_json(input_path)
            missing    = [p for p in papers if _is_missing_abstract(p)]
            total      = len(papers)
            miss_count = len(missing)

            logger.info("[%s] Scrape — Total: %d | Missing: %d", cat, total, miss_count)

            if miss_count == 0:
                _save_json(papers, output_path)
                all_stats.append((cat, {
                    "total": total, "missing": 0, "recovered": 0,
                    "per_publisher_missing": {}, "per_publisher_recovered": {},
                }))
                continue

            pub_missing: dict[str, int] = defaultdict(int)
            for p in missing:
                doi = (p.get("externalIds") or {}).get("DOI", "")
                pub_missing[_publisher_from_doi(doi)] += 1

            pub_recovered: dict[str, int] = defaultdict(int)
            recovered_count = 0
            start_time      = time.time()

            for idx, paper in enumerate(missing, start=1):
                doi    = (paper.get("externalIds") or {}).get("DOI", "")
                result = scrape_provider.recover(paper)
                elapsed = time.time() - start_time
                avg     = elapsed / idx
                eta     = int(avg * (miss_count - idx))

                if result:
                    paper["abstract"] = result.abstract
                    recovered_count  += 1
                    pub_label = result.source.replace("scrape:", "")
                    pub_recovered[pub_label] += 1
                    tag = "RECOVERED"
                else:
                    pub_label = _publisher_from_doi(doi)
                    tag = "FAILED   "

                logger.info(
                    "[%s][%4d/%-4d] %s | %-22s | ETA %5ds | %s",
                    cat, idx, miss_count, tag, pub_label, eta,
                    paper.get("title", "")[:50],
                )

            total_time = time.time() - start_time
            _save_json(papers, output_path)

            print_scrape_report(
                filename=f"{cat}.json",
                total=total,
                miss_count=miss_count,
                recovered_count=recovered_count,
                pub_missing=dict(pub_missing),
                pub_recovered=dict(pub_recovered),
                total_time=total_time,
                output_path=str(output_path),
            )

            for pub, cnt in pub_missing.items():
                combined_missing[pub]   += cnt
            for pub, cnt in pub_recovered.items():
                combined_recovered[pub] += cnt

            all_stats.append((cat, {
                "total":                   total,
                "missing":                 miss_count,
                "recovered":               recovered_count,
                "per_publisher_missing":   dict(pub_missing),
                "per_publisher_recovered": dict(pub_recovered),
            }))

        total_papers    = sum(s["total"]     for _, s in all_stats)
        total_missing   = sum(s["missing"]   for _, s in all_stats)
        total_recovered = sum(s["recovered"] for _, s in all_stats)

        print_overall_scrape_summary(
            total_papers=total_papers,
            total_missing=total_missing,
            total_recovered=total_recovered,
            combined_missing=dict(combined_missing),
            combined_recovered=dict(combined_recovered),
            output_dir=str(output_dir),
        )

    @staticmethod
    def _categories_from_dir(directory: Path) -> list[str]:
        return sorted(
            p.stem for p in directory.glob("*.json")
            if p.is_file()
        )

    def _create_dirs(self) -> None:
        for d in self._dirs.values():
            d.mkdir(parents=True, exist_ok=True)
