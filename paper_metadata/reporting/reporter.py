from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def print_acquisition_report(stats: dict, output_dir: str) -> None:
    col_w = [28, 10, 12, 12, 14, 12]
    headers = ["Category", "Raw", "IntraDupes", "AfterIntra", "InterRemoved", "FinalUnique"]

    def row_str(values):
        return "  ".join(str(v).ljust(w) for v, w in zip(values, col_w))

    separator = "-" * (sum(col_w) + 2 * (len(col_w) - 1))
    total = defaultdict(int)

    print("\n" + "=" * len(separator))
    print("SEARCH & DEDUPLICATION REPORT")
    print("=" * len(separator))
    print(row_str(headers))
    print(separator)

    for cat, s in stats.items():
        print(row_str([
            cat,
            s["raw"], s["intra_dupes"], s["after_intra"],
            s["inter_removed"], s["final_unique"],
        ]))
        for k in ("raw", "intra_dupes", "after_intra", "inter_removed", "final_unique"):
            total[k] += s[k]

    print(separator)
    print(row_str([
        "TOTAL", total["raw"], total["intra_dupes"], total["after_intra"],
        total["inter_removed"], total["final_unique"],
    ]))
    print("=" * len(separator))
    print(f"\nOutput directory : {os.path.abspath(output_dir)}\n")


def print_title_dedup_report(
    stats: dict,
    total_intra_dropped: int,
    total_inter_dropped: int,
    output_dir: str,
    report_dir: str,
) -> None:
    col_w = [28, 10, 12, 12, 12, 12]
    headers = ["Category", "Input", "IntraRemoved", "AfterIntra", "InterRemoved", "FinalUnique"]

    def row_str(values):
        return "  ".join(str(v).ljust(w) for v, w in zip(values, col_w))

    separator = "-" * (sum(col_w) + 2 * (len(col_w) - 1))
    totals = defaultdict(int)

    print("\n" + "=" * len(separator))
    print("TITLE DEDUPLICATION REPORT")
    print("=" * len(separator))
    print(row_str(headers))
    print(separator)

    for cat, s in stats.items():
        print(row_str([
            cat,
            s["input"], s["intra_removed"], s["after_intra"],
            s["inter_removed"], s["final_unique"],
        ]))
        for k in ("input", "intra_removed", "after_intra", "inter_removed", "final_unique"):
            totals[k] += s[k]

    print(separator)
    print(row_str(["TOTAL", totals["input"], totals["intra_removed"], totals["after_intra"],
                   totals["inter_removed"], totals["final_unique"]]))
    print("=" * len(separator))
    print(f"\nTotal intra-category title duplicates removed : {total_intra_dropped}")
    print(f"Total inter-category title duplicates removed : {total_inter_dropped}")
    print(f"Total title duplicates removed                : {total_intra_dropped + total_inter_dropped}")
    print(f"\nOutput directory : {os.path.abspath(output_dir)}")
    print(f"Report directory : {os.path.abspath(report_dir)}\n")



def print_recovery_report(
    total: int,
    missing_count: int,
    recovered_count: int,
    source_counts: dict[str, int],
    total_time: float,
    output_path: str,
) -> None:
    sep = "=" * 60
    print(sep)
    print("ABSTRACT RECOVERY COMPLETE")
    print(sep)
    print(f"Total papers          : {total}")
    print(f"Papers missing        : {missing_count}")
    print(f"Successfully recovered: {recovered_count}")
    print(f"Still missing         : {missing_count - recovered_count}")
    print(f"Recovery rate         : {recovered_count / missing_count * 100:.1f}%" if missing_count else "N/A")
    print(f"Total time            : {total_time / 60:.1f} min ({total_time:.0f}s)")
    print(f"Avg time per paper    : {total_time / missing_count:.1f}s" if missing_count else "")
    print("Recovered by source:")
    for source, count in sorted(source_counts.items(), key=lambda x: -x[1]):
        print(f"  {source:<20}: {count}")
    print(f"Output saved to       : {output_path}")



def print_scrape_report(
    filename: str,
    total: int,
    miss_count: int,
    recovered_count: int,
    pub_missing: dict[str, int],
    pub_recovered: dict[str, int],
    total_time: float,
    output_path: str,
) -> None:
    sep_major = "=" * 72
    sep_minor = "─" * 60

    pct = recovered_count / miss_count * 100 if miss_count > 0 else 0.0
    print(f"\n  {sep_minor}")
    print(f"  File Summary  :  {filename}")
    print(sep_minor)
    print(f"  Recovered     :  {recovered_count} / {miss_count}  ({pct:.1f}%)")
    print(f"  Still missing :  {miss_count - recovered_count} / {miss_count}")
    print(f"  Elapsed       :  {total_time / 60:.1f} min")
    print()
    print(f"  {'Publisher':<28} {'Missing':>8}  {'Recovered':>10}  {'Rate':>6}")
    print(f"  {'─' * 57}")

    all_pubs = sorted(set(list(pub_missing.keys()) + list(pub_recovered.keys())))
    for pub in all_pubs:
        m = pub_missing.get(pub, 0)
        r = pub_recovered.get(pub, 0)
        rate = f"{r / m * 100:.0f}%" if m > 0 else "  —"
        print(f"  {pub:<28} {m:>8}  {r:>10}  {rate:>6}")

    print(f"\n  Saved to : {os.path.abspath(output_path)}")


def print_overall_scrape_summary(
    total_papers: int,
    total_missing: int,
    total_recovered: int,
    combined_missing: dict[str, int],
    combined_recovered: dict[str, int],
    output_dir: str,
) -> None:
    sep_major = "=" * 72
    overall_pct = total_recovered / total_missing * 100 if total_missing > 0 else 0.0

    print(f"\n{sep_major}")
    print("OVERALL SCRAPE SUMMARY")
    print(sep_major)
    print(f"  Total papers processed  : {total_papers}")
    print(f"  Total missing abstracts : {total_missing}")
    print(f"  Total recovered         : {total_recovered}  ({overall_pct:.1f}%)")
    print(f"  Still missing           : {total_missing - total_recovered}")
    print()
    print(f"  {'Publisher':<28} {'Missing':>8}  {'Recovered':>10}  {'Rate':>6}")
    print(f"  {'─' * 57}")

    all_pubs = sorted(set(list(combined_missing.keys()) + list(combined_recovered.keys())))
    for pub in all_pubs:
        m = combined_missing.get(pub, 0)
        r = combined_recovered.get(pub, 0)
        rate = f"{r / m * 100:.0f}%" if m > 0 else "  —"
        print(f"  {pub:<28} {m:>8}  {r:>10}  {rate:>6}")

    print(f"\n  Output directory : {os.path.abspath(output_dir)}")
    print(sep_major)


def save_csv(rows: list[dict], filepath: Path, fieldnames: list[str]) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def save_stats_json(data: dict, filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)