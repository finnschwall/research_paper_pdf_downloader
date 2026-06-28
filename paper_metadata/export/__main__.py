"""
paper_metadata.export CLI
=========================
Produce a SEER ingest bundle for any of the three run types.

Usage
-----
    python -m paper_metadata.export keyword  [options]
    python -m paper_metadata.export by-id    --ids <file_or_ids> --bundle-dir <dir> [options]
    python -m paper_metadata.export citation --ids <file_or_ids> --bundle-dir <dir> [options]

See --help on each subcommand for full option list.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_ids(value: str) -> list[str]:
    """
    Accept either a path to a JSON file (list of strings) or a
    comma-separated string of IDs.
    """
    path = Path(value)
    if path.suffix.lower() == ".json" and path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise argparse.ArgumentTypeError(
                f"{value}: expected a JSON array of ID strings."
            )
        return [str(item).strip() for item in data if str(item).strip()]
    # Fall back to comma-separated
    return [s.strip() for s in value.split(",") if s.strip()]


def _cmd_preview(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3]))
    import paper_data as pd

    counts = pd.preview_queries(
        config_path=args.config,
        search_queries_path=args.search_queries_path,
    )
    col_w = [32, 10]
    sep = "-" * (sum(col_w) + 2)
    print(f"\n{'Query':<32}  {'~Results':>10}")
    print(sep)
    total = 0
    for qid, count in counts.items():
        print(f"{qid:<32}  {count:>10,}")
        total += count
    print(sep)
    print(f"{'TOTAL':<32}  {total:>10,}\n")


def _cmd_list_runs(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3]))
    import paper_data as pd

    runs = pd.list_runs(args.base_dir)
    if not runs:
        print(f"No runs found under {args.base_dir}")
        return

    col_w = [30, 18, 12, 20]
    sep = "-" * (sum(col_w) + 2 * (len(col_w) - 1))
    header = f"{'Run ID':<30}  {'Label':<18}  {'Status':<12}  {'Started':<20}"
    print(f"\n{header}")
    print(sep)
    for r in runs:
        label = r.label or ""
        total = sum(v.get("final", 0) for v in r.counts.values()) if r.counts else ""
        started = r.started_at[:19].replace("T", " ") if r.started_at else ""
        total_str = f"  [{total} papers]" if total != "" else ""
        print(f"{r.run_id:<30}  {label:<18}  {r.status:<12}  {started}{total_str}")
    print()


def _cmd_keyword(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3]))
    import paper_data as pd

    bundle_dir = pd.export_keyword_bundle(
        bundle_dir=args.bundle_dir,
        source_label=args.label,
        label=args.label,
        base_dir=args.base_dir,
        search_queries_path=args.search_queries_path,
        api_recovery=not args.no_recovery and args.api_recovery,
        scrape_recovery=not args.no_recovery and args.scrape_recovery,
    )
    print(f"Bundle written to: {bundle_dir}")


def _cmd_by_id(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3]))
    import paper_data as pd

    ids = _load_ids(args.ids)
    bundle_dir = pd.export_by_id_bundle(
        ids,
        bundle_dir=args.bundle_dir,
        source_label=args.label,
        config_path=args.config,
    )
    print(f"Bundle written to: {bundle_dir}")


def _cmd_citation(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path(__file__).parents[3]))
    import paper_data as pd

    ids = _load_ids(args.ids)
    bundle_dir = pd.export_citation_bundle(
        ids,
        bundle_dir=args.bundle_dir,
        source_label=args.label,
        citations=args.citations,
        references=args.references,
        config_path=args.config,
    )
    print(f"Bundle written to: {bundle_dir}")


def main(argv: list[str] | None = None) -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(
        prog="python -m paper_metadata.export",
        description="Produce a SEER ingest bundle.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # ── preview ───────────────────────────────────────────────────────────────
    pv = sub.add_parser("preview", help="Show estimated result counts per query without fetching.")
    pv.add_argument("--search-queries-path", dest="search_queries_path", metavar="FILE",
                    help="Path to search_queries.json (overrides config).")
    pv.add_argument("--config", metavar="FILE",
                    help="Override paper_metadata config.json location.")
    pv.set_defaults(func=_cmd_preview)

    # ── list-runs ─────────────────────────────────────────────────────────────
    lr = sub.add_parser("list-runs", help="List all runs under a base directory.")
    lr.add_argument("--base-dir", dest="base_dir", required=True, metavar="DIR",
                    help="Base directory that contains runs/.")
    lr.set_defaults(func=_cmd_list_runs)

    # ── keyword ───────────────────────────────────────────────────────────────
    kw = sub.add_parser("keyword", help="Keyword-search run (full pipeline).")
    kw.add_argument("--base-dir", dest="base_dir", metavar="DIR",
                    help="Output base directory (overrides config).")
    kw.add_argument("--bundle-dir", dest="bundle_dir", metavar="DIR", default=None,
                    help="Override default bundle output location.")
    kw.add_argument("--search-queries-path", dest="search_queries_path", metavar="FILE",
                    help="Override search_queries.json location.")
    kw.add_argument("--config", metavar="FILE",
                    help="Override paper_metadata config.json location.")
    kw.add_argument("--label", metavar="TEXT",
                    help="Human-readable label stored in manifest.source_label.")
    recovery = kw.add_mutually_exclusive_group()
    recovery.add_argument("--no-recovery", dest="no_recovery", action="store_true",
                          default=False, help="Skip all abstract recovery stages.")
    recovery.add_argument("--api-recovery-only", dest="api_recovery",
                          action="store_true", default=True,
                          help="Run API recovery only (default: both).")
    kw.set_defaults(api_recovery=True, scrape_recovery=True)
    kw.set_defaults(func=_cmd_keyword)

    # ── by-id ─────────────────────────────────────────────────────────────────
    bid = sub.add_parser("by-id", help="Explicit paper ID list.")
    bid.add_argument("--ids", required=True, metavar="FILE_OR_IDS",
                     help="JSON file with a list of IDs, or comma-separated IDs.")
    bid.add_argument("--bundle-dir", dest="bundle_dir", required=True, metavar="DIR",
                     help="Directory to write the bundle into.")
    bid.add_argument("--config", metavar="FILE",
                     help="Override paper_metadata config.json location.")
    bid.add_argument("--label", metavar="TEXT",
                     help="Human-readable label stored in manifest.source_label.")
    bid.set_defaults(func=_cmd_by_id)

    # ── citation ──────────────────────────────────────────────────────────────
    cit = sub.add_parser("citation", help="Citation/reference graph snowball.")
    cit.add_argument("--ids", required=True, metavar="FILE_OR_IDS",
                     help="JSON file with seed paper IDs, or comma-separated IDs.")
    cit.add_argument("--bundle-dir", dest="bundle_dir", required=True, metavar="DIR",
                     help="Directory to write the bundle into.")
    cit.add_argument("--citations", action="store_true", default=False,
                     help="Fetch citing papers for each seed.")
    cit.add_argument("--references", action="store_true", default=False,
                     help="Fetch referenced papers for each seed.")
    cit.add_argument("--config", metavar="FILE",
                     help="Override paper_metadata config.json location.")
    cit.add_argument("--label", metavar="TEXT",
                     help="Human-readable label stored in manifest.source_label.")
    cit.set_defaults(func=_cmd_citation)

    parsed = parser.parse_args(argv)

    # Default citation subcommand: fetch both if neither flag is given.
    if parsed.subcommand == "citation" and not parsed.citations and not parsed.references:
        parsed.citations = True
        parsed.references = True

    parsed.func(parsed)


if __name__ == "__main__":
    main()
