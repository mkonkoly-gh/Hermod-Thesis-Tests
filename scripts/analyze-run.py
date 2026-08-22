#!/usr/bin/env python3
"""
Per-run analyser. Reads one run dir under Tests/raw/, computes per-rep
metrics, plots a quicklook PNG, raises flags on threshold breaches, and
exits 1 if any flag fires.

The orchestrator calls this synchronously after each "passed" run; flag
→ pause campaign → human investigates → fix → --rerun + --resume.

Output (next to the run dir, never overwritten in place):
    <run-dir>/analysis/summary.json
    <run-dir>/analysis/per-rep.csv
    <run-dir>/analysis/quicklook.png
    <run-dir>/analysis/issues.md

Exit codes:
    0 — clean
    1 — at least one flag raised
    2 — input dir is unreadable / malformed (treat as bug, not data flag)

Usage:
    analyze-run.py <run-dir>
"""
from __future__ import annotations

import csv
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


# ----- thresholds (keep in sync with CYCLE.md) -----
LOSS_PCT_LIMIT = 5.0
CV_LIMIT_PCT = 5.0
P99_LIMIT_MS = 50.0
ACHIEVEMENT_FLOOR_PCT = 95.0
INGEST_QUEUE_LIMIT = 1000.0
CACHE_HIT_DROP_LIMIT_PCT = 1.0


@dataclass
class RepRow:
    rep: int
    rate_target: float
    rate_actual: float
    published: int
    ingested_delta: int
    cache_hits_delta: int
    cache_hit_rate_pct: float
    duration_sec: float
    effective_throughput: float
    loss_pct: float
    rule_eval_p50_ms: float | None
    rule_eval_p95_ms: float | None
    rule_eval_p99_ms: float | None
    ingest_queue_end: float | None


@dataclass
class Issue:
    rule: str
    severity: str   # "flag" → halt; "warn" → record only
    detail: str


@dataclass
class Analysis:
    run_dir: Path
    profile: str
    tag: str
    rate_target: float
    saturate_profile: bool
    reps: list[RepRow] = field(default_factory=list)
    pod_restarts: dict = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)


# -------- Prometheus parsing helpers --------

PROM_LINE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{[^}]*\})?\s+(?P<value>-?\d+(\.\d+)?(e[+\-]?\d+)?)\s*$",
                       re.MULTILINE)


def parse_prom(text: str) -> dict[tuple[str, frozenset], float]:
    """Return {(name, labelset): value} where labelset is a frozenset
    of (key, value) tuples. Untyped — caller decodes histogram_bucket."""
    out: dict[tuple[str, frozenset], float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = PROM_LINE.match(line)
        if not m:
            continue
        name = m.group("name")
        labels_raw = m.group("labels") or ""
        labels: list[tuple[str, str]] = []
        if labels_raw:
            inner = labels_raw[1:-1]
            for part in re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', inner):
                labels.append(part)
        try:
            val = float(m.group("value"))
        except ValueError:
            continue
        out[(name, frozenset(labels))] = val
    return out


def metric(p: dict, name: str, **labels: str) -> float | None:
    """Look up a single metric value — returns None if absent."""
    label_set = frozenset(labels.items())
    return p.get((name, label_set))


def histogram_quantile(p: dict, name: str, q: float) -> float | None:
    """Linear interpolation across bucket boundaries — same as
    Prometheus' histogram_quantile(). Returns latency in *seconds*.

    The Hermod histograms expose `<name>_bucket{le="<bound>"}` keys; we
    pick the entries with no labels other than `le=` (no per-rule
    splits) — the Coordinator emits them un-labelled."""
    buckets: list[tuple[float, float]] = []  # (le, count)
    for (key_name, label_fs), value in p.items():
        if key_name != name + "_bucket":
            continue
        labels = dict(label_fs)
        if "le" not in labels or len(labels) != 1:
            continue
        try:
            le = float(labels["le"]) if labels["le"] != "+Inf" else float("inf")
        except ValueError:
            continue
        buckets.append((le, value))
    if not buckets:
        return None
    buckets.sort()
    total = buckets[-1][1]
    if total <= 0:
        return None
    target = q * total
    prev_le, prev_count = 0.0, 0.0
    for le, count in buckets:
        if count >= target:
            if le == float("inf"):
                return prev_le if prev_count > 0 else None
            if count == prev_count or le == prev_le:
                return le
            frac = (target - prev_count) / (count - prev_count)
            return prev_le + frac * (le - prev_le)
        prev_le, prev_count = le, count
    return buckets[-1][0]


# -------- per-rep extraction --------

def is_saturate_profile(profile: str) -> bool:
    p = profile.lower()
    return any(s in p for s in ("saturate", "breaking", "stress", "overload"))


def collect_reps(run_dir: Path, manifest: dict) -> list[RepRow]:
    reps_total = manifest.get("repeats_total") or manifest.get("repeats", 0) or 0
    rows: list[RepRow] = []

    measure_sec = float(manifest.get("measure_sec") or 30)

    for rep in range(1, reps_total + 1):
        lg_path = run_dir / f"loadgen-r{rep}.json"
        before_path = run_dir / f"metrics-before-r{rep}.txt"
        after_path = run_dir / f"metrics-after-r{rep}.txt"
        if not (lg_path.exists() and before_path.exists() and after_path.exists()):
            continue

        lg = json.loads(lg_path.read_text())
        before = parse_prom(before_path.read_text())
        after = parse_prom(after_path.read_text())

        ingested_b = metric(before, "hermod_messages_ingested_total") or 0
        ingested_a = metric(after, "hermod_messages_ingested_total") or 0
        cache_b = metric(before, "hermod_rule_cache_hits_total") or 0
        cache_a = metric(after, "hermod_rule_cache_hits_total") or 0

        ingested_delta = max(int(ingested_a - ingested_b), 0)
        cache_delta = max(int(cache_a - cache_b), 0)
        published = int(lg.get("published", 0))
        duration = float(lg.get("duration_sec") or measure_sec or 1)

        eff_throughput = ingested_delta / duration if duration else 0.0
        loss_pct = ((published - ingested_delta) / published * 100) if published else 0.0
        cache_rate = (cache_delta / ingested_delta * 100) if ingested_delta else 0.0

        # latency p* from after_path histograms (cumulative since pod start;
        # for per-rep accuracy we'd need before/after delta of histograms,
        # which is non-trivial. Reading after-state is good enough for the
        # quick flag — campaign-final aggregation will do delta-histograms.)
        p50 = histogram_quantile(after, "hermod_rule_eval_seconds", 0.50)
        p95 = histogram_quantile(after, "hermod_rule_eval_seconds", 0.95)
        p99 = histogram_quantile(after, "hermod_rule_eval_seconds", 0.99)

        queue_end = metric(after, "hermod_ingest_queue_depth")

        rows.append(RepRow(
            rep=rep,
            rate_target=float(lg.get("rate_target") or 0),
            rate_actual=float(lg.get("rate_actual") or 0),
            published=published,
            ingested_delta=ingested_delta,
            cache_hits_delta=cache_delta,
            cache_hit_rate_pct=cache_rate,
            duration_sec=duration,
            effective_throughput=eff_throughput,
            loss_pct=loss_pct,
            rule_eval_p50_ms=p50 * 1000 if p50 is not None else None,
            rule_eval_p95_ms=p95 * 1000 if p95 is not None else None,
            rule_eval_p99_ms=p99 * 1000 if p99 is not None else None,
            ingest_queue_end=queue_end,
        ))
    return rows


# -------- flag rules --------

def cv_pct(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = statistics.mean(values)
    if mu == 0:
        return 0.0
    return statistics.stdev(values) / abs(mu) * 100


def evaluate_flags(an: Analysis) -> None:
    if not an.reps:
        an.issues.append(Issue("no-reps", "flag", "no rep data parsed"))
        return

    # restart flag from manifest pod_restarts
    for pod, count in (an.pod_restarts or {}).items():
        try:
            n = int(count)
        except Exception:
            continue
        if n > 0 and pod in ("hermod-coordinator", "nanomq", "postgres"):
            an.issues.append(Issue(
                "pod-restart", "flag",
                f"{pod} restarted {n}× during the run",
            ))

    # per-rep loss / queue / latency
    for r in an.reps:
        if not an.saturate_profile and abs(r.loss_pct) > LOSS_PCT_LIMIT:
            an.issues.append(Issue(
                "loss-anomaly", "flag",
                f"rep {r.rep}: |loss_pct| = {r.loss_pct:.2f}% > {LOSS_PCT_LIMIT}%",
            ))
        if r.ingest_queue_end is not None and r.ingest_queue_end > INGEST_QUEUE_LIMIT:
            an.issues.append(Issue(
                "ingest-queue-grow", "flag",
                f"rep {r.rep}: queue end depth {r.ingest_queue_end:.0f} > {INGEST_QUEUE_LIMIT:.0f}",
            ))
        if (not an.saturate_profile and r.rule_eval_p99_ms is not None
                and r.rule_eval_p99_ms > P99_LIMIT_MS):
            an.issues.append(Issue(
                "latency-blowup", "flag",
                f"rep {r.rep}: rule-eval p99 = {r.rule_eval_p99_ms:.2f} ms > {P99_LIMIT_MS} ms",
            ))
        if (not an.saturate_profile and r.rate_target > 0
                and (r.effective_throughput / r.rate_target * 100) < ACHIEVEMENT_FLOOR_PCT):
            an.issues.append(Issue(
                "target-undershoot", "flag",
                f"rep {r.rep}: effective {r.effective_throughput:.0f} / target {r.rate_target:.0f} = "
                f"{r.effective_throughput / r.rate_target * 100:.1f}% < {ACHIEVEMENT_FLOOR_PCT}%",
            ))

    # CV across reps (only meaningful when reps >= 3)
    if len(an.reps) >= 3:
        thru = [r.effective_throughput for r in an.reps]
        cv = cv_pct(thru)
        if cv > CV_LIMIT_PCT:
            an.issues.append(Issue(
                "cv-too-noisy", "flag",
                f"throughput CV across {len(an.reps)} reps = {cv:.2f}% > {CV_LIMIT_PCT}%",
            ))

    # cache-hit-rate stability
    rates = [r.cache_hit_rate_pct for r in an.reps if r.ingested_delta > 0]
    if len(rates) >= 2 and (max(rates) - min(rates)) > CACHE_HIT_DROP_LIMIT_PCT:
        an.issues.append(Issue(
            "cache-leak", "warn",
            f"rule_cache_hit_rate range across reps "
            f"[{min(rates):.2f} .. {max(rates):.2f}]% > {CACHE_HIT_DROP_LIMIT_PCT}%",
        ))


# -------- output --------

def write_per_rep_csv(an: Analysis, out: Path) -> None:
    cols = [f.name for f in RepRow.__dataclass_fields__.values()]
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in an.reps:
            w.writerow([getattr(r, c) for c in cols])


def write_summary_json(an: Analysis, out: Path) -> None:
    flags = [i.rule for i in an.issues if i.severity == "flag"]
    warns = [i.rule for i in an.issues if i.severity == "warn"]
    thru = [r.effective_throughput for r in an.reps]
    p99s = [r.rule_eval_p99_ms for r in an.reps if r.rule_eval_p99_ms is not None]
    out.write_text(json.dumps({
        "run_dir": str(an.run_dir),
        "profile": an.profile,
        "tag": an.tag,
        "rate_target": an.rate_target,
        "saturate_profile": an.saturate_profile,
        "reps": len(an.reps),
        "throughput_mean": (statistics.mean(thru) if thru else None),
        "throughput_cv_pct": cv_pct(thru),
        "loss_pct_max": (max(r.loss_pct for r in an.reps) if an.reps else None),
        "rule_eval_p99_ms_max": (max(p99s) if p99s else None),
        "queue_depth_end_max": max(((r.ingest_queue_end or 0) for r in an.reps), default=0),
        "pod_restarts": an.pod_restarts,
        "flags": flags,
        "warns": warns,
        "issues": [{"rule": i.rule, "severity": i.severity, "detail": i.detail} for i in an.issues],
    }, indent=2, default=str))


def write_issues_md(an: Analysis, out: Path) -> None:
    lines = [f"# {an.tag} — issues\n\n"]
    if not an.issues:
        lines.append("_clean — no flags or warnings_\n")
    else:
        flags = [i for i in an.issues if i.severity == "flag"]
        warns = [i for i in an.issues if i.severity == "warn"]
        if flags:
            lines.append("## Flags (campaign halts)\n\n")
            for i in flags:
                lines.append(f"- **{i.rule}** — {i.detail}\n")
            lines.append("\n")
        if warns:
            lines.append("## Warnings (informational, campaign continues)\n\n")
            for i in warns:
                lines.append(f"- {i.rule} — {i.detail}\n")
            lines.append("\n")
    out.write_text("".join(lines))


def plot_quicklook(an: Analysis, out: Path) -> None:
    if not an.reps:
        return
    plt.rcParams.update({"figure.dpi": 140, "savefig.dpi": 160, "font.size": 10})
    fig, (ax_thru, ax_loss, ax_lat) = plt.subplots(3, 1, figsize=(8, 9))

    reps_x = [r.rep for r in an.reps]
    thru = [r.effective_throughput for r in an.reps]

    # 1. Throughput per rep with target line.
    bar_color = ["#9cd9a8" if abs(r.loss_pct) < LOSS_PCT_LIMIT else "#fcb6a3"
                 for r in an.reps]
    ax_thru.bar(reps_x, thru, color=bar_color, edgecolor="black", linewidth=0.5)
    if an.rate_target > 0:
        ax_thru.axhline(an.rate_target, color="red", lw=1, ls="--",
                        label=f"target {an.rate_target:.0f} msg/s")
        ax_thru.axhline(an.rate_target * ACHIEVEMENT_FLOOR_PCT / 100, color="orange",
                        lw=0.7, ls=":", label=f"{ACHIEVEMENT_FLOOR_PCT}% floor")
    ax_thru.set_xlabel("rep")
    ax_thru.set_ylabel("effective msg/s")
    ax_thru.set_title(f"{an.tag}  ·  {an.profile}")
    ax_thru.legend(loc="lower right", fontsize=8)
    for x, y, r in zip(reps_x, thru, an.reps):
        ax_thru.text(x, y * 1.01, f"{y:.0f}", ha="center", fontsize=8)

    # 2. Loss per rep (signed; negative = over-publish).
    losses = [r.loss_pct for r in an.reps]
    bar_color = ["#9cd9a8" if abs(l) < LOSS_PCT_LIMIT else "#fcb6a3" for l in losses]
    ax_loss.bar(reps_x, losses, color=bar_color, edgecolor="black", linewidth=0.5)
    ax_loss.axhline(LOSS_PCT_LIMIT, color="red", lw=0.7, ls="--",
                    label=f"|loss| > {LOSS_PCT_LIMIT}% flag")
    ax_loss.axhline(-LOSS_PCT_LIMIT, color="red", lw=0.7, ls="--")
    ax_loss.axhline(0, color="black", lw=0.5)
    ax_loss.set_xlabel("rep")
    ax_loss.set_ylabel("loss %")
    ax_loss.set_title("Per-rep loss")
    ax_loss.legend(loc="lower right", fontsize=8)
    for x, y in zip(reps_x, losses):
        ax_loss.text(x, y + (0.3 if y >= 0 else -0.6), f"{y:.2f}",
                     ha="center", fontsize=8)

    # 3. p50 / p95 / p99 latency per rep.
    p50 = [r.rule_eval_p50_ms for r in an.reps]
    p95 = [r.rule_eval_p95_ms for r in an.reps]
    p99 = [r.rule_eval_p99_ms for r in an.reps]
    valid = [(rep, a, b, c) for rep, a, b, c in zip(reps_x, p50, p95, p99)
             if a is not None and b is not None and c is not None]
    if valid:
        x = [v[0] for v in valid]
        ax_lat.plot(x, [v[1] for v in valid], "o-", label="p50", color="#1f77b4")
        ax_lat.plot(x, [v[2] for v in valid], "s-", label="p95", color="#ff7f0e")
        ax_lat.plot(x, [v[3] for v in valid], "^-", label="p99", color="#d62728")
        ax_lat.axhline(P99_LIMIT_MS, color="red", lw=0.7, ls="--",
                       label=f"p99 > {P99_LIMIT_MS} ms flag")
        ax_lat.set_yscale("log")
        ax_lat.set_xlabel("rep")
        ax_lat.set_ylabel("ms (log)")
        ax_lat.set_title("Rule-eval latency (cumulative-since-start)")
        ax_lat.legend(loc="upper right", fontsize=8)
    else:
        ax_lat.text(0.5, 0.5, "no histogram data", ha="center", va="center",
                    transform=ax_lat.transAxes)
        ax_lat.set_axis_off()

    fig.suptitle(
        f"quicklook · {len(an.reps)} reps · "
        f"{'FLAGGED' if any(i.severity == 'flag' for i in an.issues) else 'clean'}",
        y=0.995, fontsize=11,
    )
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


# -------- main --------

def analyse(run_dir: Path) -> int:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"analyze-run: no manifest.json at {manifest_path}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())

    an = Analysis(
        run_dir=run_dir,
        profile=manifest.get("profile", "?"),
        tag=manifest.get("name") or manifest.get("profile", run_dir.name),
        rate_target=float(manifest.get("load", {}).get("devices", 0)
                          * manifest.get("load", {}).get("rate_per_device_hz", 0)
                          or manifest.get("rate_target") or 0),
        saturate_profile=is_saturate_profile(manifest.get("profile", "")),
        pod_restarts=manifest.get("pod_restarts", {}) or {},
    )
    # rate_override sourced from the runner takes precedence over the
    # profile's static rate (V's overlays use HERMOD_OVERRIDE_RATE).
    if manifest.get("rate_override"):
        try:
            an.rate_target = float(manifest["rate_override"])
        except (TypeError, ValueError):
            pass

    an.reps = collect_reps(run_dir, manifest)
    evaluate_flags(an)

    out_dir = run_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_summary_json(an, out_dir / "summary.json")
    write_per_rep_csv(an, out_dir / "per-rep.csv")
    write_issues_md(an, out_dir / "issues.md")
    plot_quicklook(an, out_dir / "quicklook.png")

    flagged = any(i.severity == "flag" for i in an.issues)
    if flagged:
        print(f"FLAGGED  {an.tag}  ({len([i for i in an.issues if i.severity == 'flag'])} flags)")
        for i in an.issues:
            if i.severity == "flag":
                print(f"  - {i.rule}: {i.detail}")
    else:
        print(f"clean    {an.tag}  reps={len(an.reps)}  "
              f"thru_mean={statistics.mean(r.effective_throughput for r in an.reps) if an.reps else 0:.0f}")
    return 1 if flagged else 0


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: analyze-run.py <run-dir>", file=sys.stderr)
        return 2
    return analyse(Path(sys.argv[1]).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
