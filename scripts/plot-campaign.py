#!/usr/bin/env python3
"""Cross-run campaign rollup. Reads every raw/<run>/summary.json and
produces a single overview chart at Tests/figures/campaign.png:

    rate_actual vs rate_target  (bars, sorted by target)
    drop_pct vs target          (bars, log-y)
    p50 / p99 / p99.9 vs target (lines)
    coord cpu_max vs target     (bars, with 1000m saturation line)

Idempotent. Skips runs without summary.json. Re-run any time.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "raw"
OUT_DIR = ROOT / "figures"
OUT_DIR.mkdir(exist_ok=True)


def load_summaries() -> list[dict]:
    runs: list[dict] = []
    for d in sorted(RAW.glob("2026*")):
        sj = d / "summary.json"
        if not sj.exists():
            continue
        try:
            s = json.loads(sj.read_text())
        except Exception:
            continue
        # Pull the tag back out of the run dir name. Format:
        # <ts>-<profile>-<tag>; tag is the trailing part after the
        # last segment of the profile name.
        name = d.name
        s["_dir"] = name
        # Best effort tag extraction.
        tag = name.split("-pi5-live-8gb-", 1)[-1]
        s["_tag"] = tag
        runs.append(s)
    return runs


def main() -> int:
    runs = load_summaries()
    if not runs:
        print("no summaries yet")
        return 0
    # Sort by target rate then tag for readable plots.
    runs.sort(key=lambda r: (r.get("throughput", {}).get("target_msg_per_sec", 0), r["_tag"]))
    tags = [r["_tag"] for r in runs]
    targets = [r["throughput"].get("target_msg_per_sec", 0) for r in runs]
    actual = [r["throughput"].get("mean_rate", 0) for r in runs]
    pubs = [r["throughput"].get("published_total", 0) for r in runs]
    drops = [r.get("coord_metrics", {}).get("messages_dropped_total", 0) for r in runs]
    drop_pct = [r.get("coord_metrics", {}).get("dropped_pct_of_published", 0) for r in runs]
    p50 = [r.get("latency_ms", {}).get("p50", 0) for r in runs]
    p99 = [r.get("latency_ms", {}).get("p99", 0) for r in runs]
    p999 = [r.get("latency_ms", {}).get("p999", 0) for r in runs]
    cpu_max = [r.get("resources", {}).get("hermod-coordinator", {}).get("cpu_max_m", 0) for r in runs]
    n = len(runs)
    x = list(range(n))
    fig, axs = plt.subplots(4, 1, figsize=(max(10, n * 0.5), 13), sharex=True)
    fig.suptitle(f"Hermod thesis campaign rollup — {n} passed runs (Pi5 8GB)",
                 fontsize=13, fontweight="bold", y=0.997)
    # 1. Throughput
    ax = axs[0]
    width = 0.4
    ax.bar([i - width/2 for i in x], targets, width, label="target", color="#bdbdbd")
    ax.bar([i + width/2 for i in x], actual, width, label="actual", color="#2c7fb8")
    ax.set_ylabel("msg/s")
    ax.set_title("rate: target vs actual")
    ax.legend(loc="upper left")
    ax.grid(axis="y", alpha=0.3)
    # 2. Drop % (log-y so 0 vs 1% vs 20% all readable)
    ax = axs[1]
    colors = ["#1a9850" if (t < 4000 and d == 0) or (d < 1) else "#fdae61" if d < 5 else "#d73027"
              for t, d in zip(targets, drop_pct)]
    ax.bar(x, drop_pct, color=colors)
    ax.axhline(1.0, color="gray", linestyle=":", alpha=0.5, label="1% threshold")
    ax.set_ylabel("drop % of published")
    ax.set_yscale("symlog", linthresh=0.01)
    ax.set_title("coord drops vs target rate (log-scale)")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper left")
    # 3. Latency percentiles
    ax = axs[2]
    ax.plot(x, p50, marker="o", label="p50", color="#1a9850")
    ax.plot(x, p99, marker="s", label="p99", color="#fdae61")
    ax.plot(x, p999, marker="^", label="p99.9", color="#d73027")
    ax.set_ylabel("rule_eval latency (ms)")
    ax.set_yscale("symlog", linthresh=0.5)
    ax.set_title("rule_eval latency percentiles vs target")
    ax.legend(loc="upper left")
    ax.grid(axis="y", alpha=0.3)
    # 4. Coord CPU max
    ax = axs[3]
    bar_colors = ["#d73027" if c >= 950 else "#fdae61" if c >= 700 else "#1a9850"
                  for c in cpu_max]
    ax.bar(x, cpu_max, color=bar_colors)
    ax.axhline(1000, color="#d73027", linestyle=":", alpha=0.6, label="cpu limit (1000m)")
    ax.set_ylabel("coord CPU max (m)")
    ax.set_title("coord CPU peak per run")
    ax.set_xticks(x)
    ax.set_xticklabels(tags, rotation=80, fontsize=7)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper left")
    plt.tight_layout()
    out = OUT_DIR / "campaign.png"
    plt.savefig(out, dpi=110, bbox_inches="tight")
    plt.close()
    print(f"wrote {out}")
    # Also write a flat CSV for later spreadsheet work.
    csv_path = OUT_DIR / "campaign.csv"
    with csv_path.open("w") as f:
        f.write("tag,target,actual,published,dropped,drop_pct,p50_ms,p99_ms,p999_ms,cpu_max_m\n")
        for i in range(n):
            f.write(f"{tags[i]},{targets[i]},{actual[i]:.1f},{pubs[i]},{drops[i]},{drop_pct[i]:.4f},"
                    f"{p50[i]:.3f},{p99[i]:.3f},{p999[i]:.3f},{cpu_max[i]:.0f}\n")
    print(f"wrote {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
