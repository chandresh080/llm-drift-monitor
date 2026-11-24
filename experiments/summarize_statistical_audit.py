"""Turn sampling_design_calibration.py CSVs into readable Markdown tables.

This is a reporting helper only; it does not run any detector or modify data.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def pct(row: Dict[str, str]) -> str:
    rate = float(row["rate"]) * 100.0
    low = float(row["ci_low"]) * 100.0
    high = float(row["ci_high"]) * 100.0
    return f"{rate:.1f}% [{low:.1f}, {high:.1f}]"


def table(headers: List[str], rows: List[List[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=str(ROOT / "results" /
                                            "statistical_audit"))
    args = parser.parse_args()
    folder = Path(args.dir)
    null = read_csv(folder / "null_type_i.csv")
    power = read_csv(folder / "power.csv")
    alerts = read_csv(folder / "alert_rates.csv")
    delays = read_csv(folder / "detection_delay.csv")
    summary_path = folder / "summary.json"
    meta = json.loads(summary_path.read_text()) if summary_path.exists() else {}

    chunks: List[str] = [
        "# Statistical audit benchmark summary",
        "",
        "Synthetic Monte Carlo results only; not observations from the "
        "hosted service.",
        "",
        f"Commit: `{meta.get('commit')}`",
        f"Trials: {meta.get('trials')}",
        f"Runtime: {meta.get('elapsed_seconds')} s",
        "",
        "## A. Null type-I error (BH-adjusted flags)",
        "",
    ]

    grouped: Dict[tuple, List[Dict[str, str]]] = defaultdict(list)
    for row in null:
        grouped[(row["magnitude"], row["detector"])].append(row)
    rows = []
    for (magnitude, detector), entries in sorted(grouped.items()):
        for e in entries:
            rows.append([magnitude, e["method"], detector,
                         pct(e), e["rejections"], e["trials"],
                         e["mean_ms_per_window"]])
    chunks.append(table(["correlation", "method", "detector", "rate [95% CI]",
                         "k", "n", "ms/window"], rows))
    chunks.extend(["", "## B. Power by shift condition", ""])

    grouped = defaultdict(list)
    for row in power:
        grouped[(row["condition"], row["magnitude"], row["detector"])].append(row)
    rows = []
    for (condition, magnitude, detector), entries in grouped.items():
        for e in sorted(entries, key=lambda x: x["method"]):
            rows.append([condition, magnitude, detector, e["method"], pct(e),
                         e["rejections"], e["trials"], e["mean_ms_per_window"]])
    chunks.append(table(["condition", "magnitude", "detector", "method",
                         "rate [95% CI]", "k", "n", "ms/window"], rows))
    chunks.extend(["", "## C. Two-cycle alert policy", ""])
    chunks.append(table(
        ["condition", "correlation", "method", "rate [95% CI]", "k", "n"],
        [[r["condition"], r["magnitude"], r["method"], pct(r),
          r["rejections"], r["trials"]] for r in alerts]))
    chunks.extend(["", "## D. Detection delay under combined shift", ""])
    chunks.append(table(
        ["correlation", "method", "detected", "sequences", "rate [95% CI]",
         "mean delay (cycles)"],
        [[r["rho"], r["method"], r["detected"], r["sequences"], pct(r),
          r["mean_delay_cycles"]] for r in delays]))

    (folder / "SUMMARY.md").write_text("\n".join(chunks) + "\n")
    print(f"wrote {folder / 'SUMMARY.md'}")


if __name__ == "__main__":
    main()
