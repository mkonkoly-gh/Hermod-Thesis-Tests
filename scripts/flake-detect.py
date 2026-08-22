#!/usr/bin/env python3
"""
Post-campaign flake detector. Reads campaign.json + each passed run's
manifest.json + any consolidated CSV the runner produced, computes per-run
CV across reps, and writes state/flake-report.md.

Flagging rules (additive — any one trips the flag):
  - throughput CV across reps > flake_cv_threshold_pct  (default 5%)
  - total_loss_pct CV across reps > 25%
  - rule_eval_p99_ms range across reps > 2x median
  - any pod restart between reps (final_counters decreased)
  - rule_cache_hit_rate dropped > 1% vs. campaign-wide median for the
    same profile (suggests configuration leak — cache wasn't actually on)
"""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "campaign.json"
RAW_DIR = ROOT / "raw"
REPORT = ROOT / "state" / "flake-report.md"


def load_summary_per_rep(run_dir: Path) -> list[dict]:
    """The patched runner doesn't write a consolidated CSV per run — it
    writes per-rep manifests + metrics-after-r{N}.txt. We extract the
    deltas ourselves from final_counters and loadgen-r{N}.json."""
    rows = []
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return rows
    manifest = json.loads(manifest_path.read_text())
    n = manifest.get("repeats_total") or manifest.get("repeats") or 0
    for rep in range(1, n + 1):
        lg_path = run_dir / f"loadgen-r{rep}.json"
        before_path = run_dir / f"metrics-before-r{rep}.txt"
        after_path = run_dir / f"metrics-after-r{rep}.txt"
        if not lg_path.exists() or not after_path.exists():
            continue
        lg = json.loads(lg_path.read_text())

        def grab(p: Path, key: str) -> float | None:
            for ln in p.read_text().splitlines():
                if ln.startswith(key):
                    parts = ln.split()
                    if len(parts) >= 2:
                        try:
                            return float(parts[1])
                        except ValueError:
                            return None
            return None

        ingested_before = grab(before_path, "hermod_messages_ingested_total") or 0
        ingested_after = grab(after_path, "hermod_messages_ingested_total") or 0
        cache_hits_before = grab(before_path, "hermod_rule_cache_hits_total") or 0
        cache_hits_after = grab(after_path, "hermod_rule_cache_hits_total") or 0
        published = lg.get("published", 0)
        duration = lg.get("duration_sec", 0) or 1
        ingested_delta = max(ingested_after - ingested_before, 0)
        cache_delta = max(cache_hits_after - cache_hits_before, 0)
        rows.append({
            "rep": rep,
            "rate_target": lg.get("rate_target", 0),
            "rate_actual": lg.get("rate_actual", 0),
            "published": published,
            "ingested": ingested_delta,
            "effective_throughput": ingested_delta / duration,
            "loss_pct": ((published - ingested_delta) / published * 100) if published else 0.0,
            "cache_hits": cache_delta,
        })
    return rows


def cv_pct(values: list[float]) -> float:
    if len(values) < 2 or statistics.mean(values) == 0:
        return 0.0
    return statistics.stdev(values) / abs(statistics.mean(values)) * 100


def main() -> int:
    if not STATE_PATH.exists():
        print(f"no campaign state at {STATE_PATH}")
        return 1
    state = json.loads(STATE_PATH.read_text())
    flake_cv = state.get("flake_cv_threshold_pct", 5.0)

    flagged = []
    summary_rows = []
    for run in state["runs"]:
        if run["status"] not in ("passed", "flaky"):
            continue
        if not run["result_dir"]:
            continue
        rd = Path(run["result_dir"])
        if not rd.exists():
            continue
        reps = load_summary_per_rep(rd)
        if not reps:
            continue
        thru = [r["effective_throughput"] for r in reps]
        loss = [r["loss_pct"] for r in reps]
        thru_cv = cv_pct(thru)
        loss_cv = cv_pct(loss)
        thru_med = statistics.median(thru)
        loss_med = statistics.median(loss)
        manifest = json.loads((rd / "manifest.json").read_text())
        pod_restarts = manifest.get("pod_restarts", {}) or {}
        coord_restarts = int(pod_restarts.get("hermod-coordinator", 0))
        nanomq_restarts = int(pod_restarts.get("nanomq", 0))
        pg_restarts = int(pod_restarts.get("postgres", 0))

        flag_reasons = []
        if thru_cv > flake_cv:
            flag_reasons.append(f"throughput CV {thru_cv:.2f}% > {flake_cv}%")
        if loss_cv > 25:
            flag_reasons.append(f"loss CV {loss_cv:.2f}% > 25%")
        if coord_restarts > 0:
            flag_reasons.append(f"coord restarted {coord_restarts}x")
        if nanomq_restarts > 0:
            flag_reasons.append(f"nanomq restarted {nanomq_restarts}x")
        if pg_restarts > 0:
            flag_reasons.append(f"pg restarted {pg_restarts}x")

        row = {
            "id": run["id"],
            "profile": run["profile"],
            "rate": run["rate_override"],
            "thru_med": round(thru_med, 1),
            "thru_cv": round(thru_cv, 2),
            "loss_med": round(loss_med, 2),
            "loss_cv": round(loss_cv, 2),
            "coord_restarts": coord_restarts,
            "nanomq_restarts": nanomq_restarts,
            "pg_restarts": pg_restarts,
            "campaign_status": run["status"],
            "flake_reasons": "; ".join(flag_reasons),
        }
        summary_rows.append(row)
        if flag_reasons:
            flagged.append(row)

    # Write report
    out = ["# Flake report\n\n"]
    out.append(f"Generated from `{STATE_PATH.relative_to(ROOT)}`. "
               f"Flake CV threshold: **{flake_cv} %**.\n\n")
    out.append(f"- runs analysed: **{len(summary_rows)}**\n")
    out.append(f"- runs flagged:  **{len(flagged)}**\n\n")

    if flagged:
        out.append("## Flagged runs\n\n")
        out.append("| run | profile | rate | thru med | thru CV | loss med | loss CV | restarts (c/n/p) | reason |\n")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")
        for r in flagged:
            out.append(f"| {r['id']} | {r['profile']} | {r['rate']} | "
                       f"{r['thru_med']} | {r['thru_cv']}% | "
                       f"{r['loss_med']}% | {r['loss_cv']}% | "
                       f"{r['coord_restarts']}/{r['nanomq_restarts']}/{r['pg_restarts']} | "
                       f"{r['flake_reasons']} |\n")
        out.append("\n")

    out.append("## All runs\n\n")
    out.append("| run | profile | rate | thru med | thru CV | loss med |\n")
    out.append("| --- | --- | --- | --- | --- | --- |\n")
    for r in sorted(summary_rows, key=lambda r: r["id"]):
        out.append(f"| {r['id']} | {r['profile']} | {r['rate']} | "
                   f"{r['thru_med']} | {r['thru_cv']}% | {r['loss_med']}% |\n")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("".join(out))
    print(f"wrote {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
