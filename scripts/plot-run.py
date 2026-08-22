#!/usr/bin/env python3
"""Generate per-run plots: throughput, latency CDF, CPU/MEM timeseries.

Usage:
    python3 scripts/plot-run.py <run-dir>

Reads:
    <run-dir>/loadgen-r{1..N}.json
    <run-dir>/metrics-after-r{1..N}.txt
    <run-dir>/metrics-baseline.txt
    <run-dir>/top.tsv

Writes:
    <run-dir>/plot-throughput.png
    <run-dir>/plot-latency-cdf.png
    <run-dir>/plot-resources.png
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    print("matplotlib not installed; skipping plots", file=sys.stderr)
    sys.exit(0)


def parse_metrics(path: Path) -> dict:
    """Return {metric_name: float | dict} from a Prometheus exposition text."""
    out: dict = {}
    if not path.exists() or path.stat().st_size == 0:
        return out
    bucket_re = re.compile(r'^(\w+)_bucket\{le="([^"]+)"\}\s+([\d.eE+-]+)$')
    plain_re  = re.compile(r'^(\w+(?:_(?:total|count|sum))?)\s+([\d.eE+-]+)$')
    for line in path.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = bucket_re.match(line)
        if m:
            name, le, val = m.group(1), m.group(2), float(m.group(3))
            out.setdefault(name + "_bucket", {})[le] = val
            continue
        m = plain_re.match(line)
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def plot_throughput(run_dir: Path, reps: int) -> None:
    pubs, errs, rates, dropped_per_rep, ingested_per_rep = [], [], [], [], []
    target = 0
    prev_dropped = prev_ingested = 0
    for r in range(1, reps + 1):
        f = run_dir / f"loadgen-r{r}.json"
        if not f.exists() or f.stat().st_size == 0:
            continue
        d = json.loads(f.read_text())
        pubs.append(d.get("published", 0))
        errs.append(d.get("errors", 0))
        rates.append(d.get("rate_actual", 0))
        target = d.get("rate_target", 0) or target
        # Coord-side counters from metrics-after-r{N} are cumulative, so
        # we diff against the previous rep to get per-rep drops/ingest.
        m = parse_metrics(run_dir / f"metrics-after-r{r}.txt")
        cur_dropped = int(m.get("hermod_messages_dropped_total", 0) or 0)
        cur_ingested = int(m.get("hermod_messages_ingested_total", 0) or 0)
        dropped_per_rep.append(max(0, cur_dropped - prev_dropped))
        ingested_per_rep.append(max(0, cur_ingested - prev_ingested))
        prev_dropped, prev_ingested = cur_dropped, cur_ingested
    if not rates:
        return
    fig, ax = plt.subplots(3, 1, figsize=(8, 7), sharex=True)
    x = list(range(1, len(rates) + 1))
    ax[0].bar(x, rates, color="#2c7fb8", label="rate_actual")
    if target:
        ax[0].axhline(target, color="gray", linestyle=":", alpha=0.5,
                      label=f"target={int(target)}")
    ax[0].set_ylabel("msg/s")
    ax[0].set_title(f"{run_dir.name} — throughput")
    ax[0].legend(loc="lower right")
    ax[0].grid(axis="y", alpha=0.3)
    ax[1].bar(x, errs, color="#d7301f", label="loadgen errors")
    ax[1].set_ylabel("loadgen errors")
    ax[1].grid(axis="y", alpha=0.3)
    if any(dropped_per_rep):
        ax[2].bar(x, dropped_per_rep, color="#fdae61", label="coord dropped")
    else:
        ax[2].bar(x, dropped_per_rep, color="#bdbdbd")
    ax[2].set_ylabel("coord dropped")
    ax[2].set_xlabel("repeat")
    ax[2].grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(run_dir / "plot-throughput.png", dpi=100)
    plt.close()


def plot_latency(run_dir: Path, reps: int) -> None:
    """Per-bucket histogram of rule_eval latency on log-x. Annotates p50/p95/
    p99/p99.9 + mean directly on the plot. Diff vs baseline shows just the
    measurement window, not bucket totals dragged up by warmup traffic."""
    last = run_dir / f"metrics-after-r{reps}.txt"
    base = run_dir / "metrics-baseline.txt"
    if not last.exists() or last.stat().st_size == 0:
        return
    m_after = parse_metrics(last)
    m_base = parse_metrics(base) if base.exists() else {}
    b_after = m_after.get("hermod_rule_eval_seconds_bucket") or {}
    b_base = m_base.get("hermod_rule_eval_seconds_bucket") or {}
    if not b_after:
        return
    edges = sorted(float(le) for le in b_after if le != "+Inf")
    # Per-bucket counts = (after - baseline) cumulative diff
    cum_after = [b_after.get(str(le).rstrip("0").rstrip(".") if "." in str(le) else str(le), 0)
                 if isinstance(b_after, dict) else 0 for le in edges]
    # Robustly look up by string key — kubectl exposes "0.001" not "1e-3":
    def lookup(d, le):
        for k, v in d.items():
            try:
                if abs(float(k) - le) < 1e-12: return v
            except ValueError:
                pass
        return 0
    cum_after = [lookup(b_after, le) for le in edges]
    cum_base = [lookup(b_base, le) for le in edges] if b_base else [0]*len(edges)
    # Differential per bucket: messages whose le ≤ edge[i] minus those ≤ edge[i-1]
    per_bucket_after = [cum_after[0]] + [cum_after[i] - cum_after[i-1] for i in range(1, len(cum_after))]
    per_bucket_base  = [cum_base[0]]  + [cum_base[i]  - cum_base[i-1]  for i in range(1, len(cum_base))]
    per_bucket = [max(0, a - b) for a, b in zip(per_bucket_after, per_bucket_base)]
    total_after = m_after.get("hermod_rule_eval_seconds_count", 0)
    total_base = m_base.get("hermod_rule_eval_seconds_count", 0)
    n = int(total_after - total_base) if total_after >= total_base else int(total_after)
    s_after = m_after.get("hermod_rule_eval_seconds_sum", 0)
    s_base = m_base.get("hermod_rule_eval_seconds_sum", 0)
    s_diff = s_after - s_base
    mean_ms = (s_diff / n * 1000) if n else 0
    # Percentiles from the diff distribution.
    cum = 0
    diff_cum = []
    for c in per_bucket:
        cum += c
        diff_cum.append(cum)
    def pct(target_pct):
        tgt = n * target_pct
        prev_le, prev_count = 0.0, 0.0
        for le, cnt in zip(edges, diff_cum):
            if cnt >= tgt:
                frac = (tgt - prev_count) / (cnt - prev_count) if cnt > prev_count else 0
                return prev_le + frac * (le - prev_le)
            prev_le, prev_count = le, cnt
        return edges[-1]
    p50 = pct(0.50) * 1000
    p95 = pct(0.95) * 1000
    p99 = pct(0.99) * 1000
    p999 = pct(0.999) * 1000
    fig, ax = plt.subplots(figsize=(9, 5))
    # Bar plot over log-spaced buckets. Width adapts so the bars look right.
    log_edges = [le * 1000 for le in edges]   # ms
    bar_x = []
    bar_w = []
    for i, le in enumerate(log_edges):
        prev = log_edges[i-1] if i > 0 else le * 0.5
        bar_x.append((prev + le) / 2)
        bar_w.append(le - prev)
    ax.bar(bar_x, per_bucket, width=bar_w, color="#2c7fb8", edgecolor="white",
           align="center", alpha=0.85, log=False)
    ax.set_xscale("log")
    ax.set_xlabel("rule_eval latency (ms)")
    ax.set_ylabel("# messages")
    ax.set_title(f"{run_dir.name} — rule_eval latency histogram (n={n:,})")
    # Vertical percentile guides.
    for pct_val, label, color in [(p50, "p50", "#1a9850"),
                                  (p95, "p95", "#fdae61"),
                                  (p99, "p99", "#f46d43"),
                                  (p999, "p99.9", "#d73027")]:
        ax.axvline(pct_val, color=color, linestyle="--", alpha=0.85,
                   label=f"{label} = {pct_val:.2f} ms")
    ax.legend(loc="upper right", fontsize=9, framealpha=0.95)
    ax.grid(axis="y", alpha=0.3)
    # Stats panel inside the plot.
    txt = (f"mean   {mean_ms:>6.2f} ms\n"
           f"p50    {p50:>6.2f} ms\n"
           f"p95    {p95:>6.2f} ms\n"
           f"p99    {p99:>6.2f} ms\n"
           f"p99.9  {p999:>6.2f} ms\n"
           f"n       {n:,}")
    ax.text(0.02, 0.97, txt, transform=ax.transAxes, va="top", ha="left",
            fontfamily="monospace", fontsize=9,
            bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                      edgecolor="#888", alpha=0.92))
    plt.tight_layout()
    plt.savefig(run_dir / "plot-latency.png", dpi=100)
    plt.close()


def plot_resources(run_dir: Path) -> None:
    top = run_dir / "top.tsv"
    if not top.exists() or top.stat().st_size == 0:
        return
    series: dict[str, dict[str, list[float]]] = {}
    rows = top.read_text().splitlines()
    for line in rows[1:]:
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        ts = int(parts[0])
        pod = parts[2]
        cpu_str = parts[3]
        mem_str = parts[4]
        if not pod or pod == "pod":
            continue
        cpu = float(re.sub(r"[^\d.]", "", cpu_str) or 0)   # millicores
        mem = float(re.sub(r"[^\d.]", "", mem_str) or 0)   # Mi
        # Strip the deployment hash so all replicas group cleanly.
        short = re.sub(r"-[a-f0-9]{8,10}-[a-z0-9]{5}$", "", pod)
        s = series.setdefault(short, {"ts": [], "cpu": [], "mem": []})
        s["ts"].append(ts)
        s["cpu"].append(cpu)
        s["mem"].append(mem)
    if not series:
        return
    t0 = min(min(v["ts"]) for v in series.values())
    fig, ax = plt.subplots(2, 1, figsize=(8, 5), sharex=True)
    for pod, v in sorted(series.items()):
        rel = [t - t0 for t in v["ts"]]
        ax[0].plot(rel, v["cpu"], label=pod, alpha=0.85)
        ax[1].plot(rel, v["mem"], label=pod, alpha=0.85)
    ax[0].set_ylabel("CPU (millicores)")
    ax[0].set_title(f"{run_dir.name} — resources")
    ax[0].legend(fontsize=8, loc="upper right")
    ax[0].grid(alpha=0.3)
    ax[1].set_ylabel("Memory (MiB)")
    ax[1].set_xlabel("seconds since start")
    ax[1].grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(run_dir / "plot-resources.png", dpi=100)
    plt.close()


def write_summary(run_dir: Path, reps: int) -> None:
    """Per-run summary.json: rate stats + latency percentiles + resource peaks."""
    out: dict = {"run": run_dir.name, "reps": reps}
    # Throughput.
    rates, pubs, errs = [], [], []
    for r in range(1, reps + 1):
        f = run_dir / f"loadgen-r{r}.json"
        if not f.exists() or f.stat().st_size == 0:
            continue
        d = json.loads(f.read_text())
        rates.append(d.get("rate_actual", 0))
        pubs.append(d.get("published", 0))
        errs.append(d.get("errors", 0))
    if rates:
        target = (json.loads((run_dir / "loadgen-r1.json").read_text()).get("rate_target", 0)
                  if (run_dir / "loadgen-r1.json").exists() else 0)
        out["throughput"] = {
            "target_msg_per_sec": target,
            "rate_actual_per_rep": rates,
            "published_per_rep": pubs,
            "errors_per_rep": errs,
            "errors_total": sum(errs),
            "published_total": sum(pubs),
            "mean_rate": sum(rates) / len(rates),
            "min_rate": min(rates),
            "max_rate": max(rates),
        }
    # Coord-side metrics from last metrics-after file. coord_metrics
    # (ingest/drop counters) are always available when /metrics scraped
    # successfully; latency_ms requires the rule_eval histogram which is
    # empty under Storage=Noop (no rules in cache → no eval) so emit them
    # independently.
    last = run_dir / f"metrics-after-r{reps}.txt"
    if last.exists() and last.stat().st_size:
        m = parse_metrics(last)
        out["coord_metrics"] = {
            "messages_ingested_total": int(m.get("hermod_messages_ingested_total", 0) or 0),
            "messages_dropped_total": int(m.get("hermod_messages_dropped_total", 0) or 0),
        }
        b = m.get("hermod_rule_eval_seconds_bucket")
        total = m.get("hermod_rule_eval_seconds_count")
        s = m.get("hermod_rule_eval_seconds_sum")
        if b and total and s:
            buckets = sorted([(float(le), v) for le, v in b.items() if le != "+Inf"])
            def pct(target_pct: float) -> float:
                tgt = total * target_pct
                prev_le, prev_count = 0.0, 0.0
                for le, cnt in buckets:
                    if cnt >= tgt:
                        frac = (tgt - prev_count) / (cnt - prev_count) if cnt > prev_count else 0
                        return prev_le + frac * (le - prev_le)
                    prev_le, prev_count = le, cnt
                return buckets[-1][0]
            out["latency_ms"] = {
                "n": int(total),
                "mean": s / total * 1000,
                "p50": pct(0.50) * 1000,
                "p95": pct(0.95) * 1000,
                "p99": pct(0.99) * 1000,
                "p999": pct(0.999) * 1000,
            }
    # Resource peaks from top.tsv.
    top = run_dir / "top.tsv"
    if top.exists() and top.stat().st_size:
        agg: dict = {}
        for line in top.read_text().splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            pod = parts[2]
            cpu = float(re.sub(r"[^\d.]", "", parts[3]) or 0)
            mem = float(re.sub(r"[^\d.]", "", parts[4]) or 0)
            short = re.sub(r"-[a-f0-9]{8,10}-[a-z0-9]{5}$", "", pod)
            a = agg.setdefault(short, {"cpu": [], "mem": []})
            a["cpu"].append(cpu)
            a["mem"].append(mem)
        out["resources"] = {
            pod: {
                "cpu_avg_m": sum(v["cpu"]) / len(v["cpu"]),
                "cpu_max_m": max(v["cpu"]),
                "mem_avg_mib": sum(v["mem"]) / len(v["mem"]),
                "mem_max_mib": max(v["mem"]),
                "samples": len(v["cpu"]),
            }
            for pod, v in agg.items() if v["cpu"]
        }
    # Anomaly detection — flag conditions that warrant investigation.
    # Thresholds match V's thesis criteria: <4k must be clean, drops/loss
    # >0% under saturation are anomalies, p99 latency under 1ms is the
    # nominal envelope.
    issues: list[str] = []
    t = out.get("throughput", {})
    target = t.get("target_msg_per_sec", 0)
    if t:
        if t["errors_total"] > 0:
            issues.append(f"loadgen errors: {t['errors_total']} (any non-zero is bad)")
        rate_err = abs(t["mean_rate"] - target) / target if target else 0
        if rate_err > 0.02:
            issues.append(f"target undershoot: actual {t['mean_rate']:.0f} vs target {target:.0f} ({rate_err*100:.1f}% off)")
    cm = out.get("coord_metrics", {})
    pub = t.get("published_total", 0) or 1
    drop_pct = 100.0 * cm.get("messages_dropped_total", 0) / pub if pub else 0
    if cm:
        out["coord_metrics"]["dropped_pct_of_published"] = drop_pct
        if target and target < 4000 and cm.get("messages_dropped_total", 0) > 0:
            issues.append(f"coord drops at sub-saturation: {cm['messages_dropped_total']} dropped @ {target:.0f} msg/s")
        ingested_gap = pub - cm.get("messages_ingested_total", 0)
        if ingested_gap > pub * 0.005:
            issues.append(f"broker→coord gap: {ingested_gap} messages published but not ingested ({100*ingested_gap/pub:.2f}%)")
    lat = out.get("latency_ms", {})
    if lat:
        if target and target < 4000 and lat["p99"] > 5.0:
            issues.append(f"p99 latency high at sub-saturation: {lat['p99']:.2f}ms (expect <1ms)")
        if lat["p999"] > 100.0:
            issues.append(f"p99.9 latency tail very long: {lat['p999']:.1f}ms")
    res = out.get("resources", {})
    coord = res.get("hermod-coordinator")
    if coord and coord["cpu_max_m"] >= 1450:
        issues.append(f"coord CPU saturated: max {coord['cpu_max_m']:.0f}m (limit 1500m) — at ceiling")
    out["anomalies"] = issues
    (run_dir / "summary.json").write_text(json.dumps(out, indent=2))
    if issues:
        lines = [f"# Anomalies for {run_dir.name}", ""] + [f"- {i}" for i in issues]
        (run_dir / "issues.md").write_text("\n".join(lines) + "\n")


def plot_dashboard(run_dir: Path, reps: int) -> None:
    """One-page dashboard: header stats + throughput per rep + latency
    histogram + CPU/MEM timeline. The summary.json must already have
    been written before this function is called."""
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        return
    s = json.loads(summary_path.read_text())
    t = s.get("throughput", {})
    cm = s.get("coord_metrics", {})
    lat = s.get("latency_ms", {})
    res = s.get("resources", {})
    fig = plt.figure(figsize=(12, 9))
    gs = fig.add_gridspec(3, 2, height_ratios=[1.1, 1, 1.4], hspace=0.45, wspace=0.22)
    # Title.
    title = run_dir.name.replace("baseline-pi5-live-8gb-", "").replace("-pi5-live-8gb-", "-")
    target = t.get("target_msg_per_sec", 0)
    fig.suptitle(f"{title}    target={int(target)} msg/s    n={lat.get('n', 0):,}",
                 fontsize=13, fontweight="bold", y=0.995)
    # ------- top row: KPIs and verdicts ---------
    ax_kpi = fig.add_subplot(gs[0, :])
    ax_kpi.axis("off")
    pub = t.get("published_total", 0)
    ing = cm.get("messages_ingested_total", 0)
    drop = cm.get("messages_dropped_total", 0)
    drop_pct = cm.get("dropped_pct_of_published", 0)
    err = t.get("errors_total", 0)
    rate_mean = t.get("mean_rate", 0)
    rate_dev = abs(rate_mean - target) / target * 100 if target else 0
    # Thesis-claim verdicts: tied to RQ1/RQ10 (no drops at sub-saturation,
    # graceful at saturation), RQ7/RQ8 (no lost messages → ingested == pub).
    def verdict(ok: bool) -> str:
        return "PASS" if ok else "FAIL"
    def vc(ok: bool) -> str:
        return "#1a9850" if ok else "#d73027"
    rate_ok = rate_dev < 2.0
    drop_ok = (drop == 0) if (target and target < 4000) else (drop_pct < 1.0)
    err_ok = err == 0
    p99 = lat.get("p99", 0)
    p99_ok = (p99 < 5.0) if (target and target < 4000) else True
    coord = res.get("hermod-coordinator", {})
    cpu_max = coord.get("cpu_max_m", 0)
    cpu_sat = cpu_max >= 1450
    mem_max = coord.get("mem_max_mib", 0)
    text_blocks = [
        ("THROUGHPUT",
         f"target {int(target)} msg/s\n"
         f"actual {rate_mean:.1f} msg/s ({rate_dev:+.2f}%)\n"
         f"published {pub:,}\n"
         f"loadgen errors {err}",
         vc(rate_ok and err_ok)),
        ("INGEST / DROPS",
         f"ingested {ing:,}\n"
         f"dropped  {drop:,}\n"
         f"loss     {drop_pct:.3f} % of published\n"
         f"verdict  {verdict(drop_ok)}",
         vc(drop_ok)),
        ("LATENCY (rule_eval)",
         f"mean   {lat.get('mean', 0):.2f} ms\n"
         f"p50    {lat.get('p50', 0):.2f} ms\n"
         f"p99    {p99:.2f} ms\n"
         f"p99.9  {lat.get('p999', 0):.2f} ms",
         vc(p99_ok)),
        ("COORD RESOURCES",
         f"cpu max  {cpu_max:.0f} m of 1500m\n"
         f"cpu avg  {coord.get('cpu_avg_m', 0):.0f} m\n"
         f"mem max  {mem_max:.0f} MiB of 1536MiB\n"
         f"saturated {'yes' if cpu_sat else 'no'}",
         "#fdae61" if cpu_sat else "#1a9850"),
    ]
    for i, (heading, body, color) in enumerate(text_blocks):
        x = 0.005 + i * 0.252
        ax_kpi.add_patch(plt.Rectangle((x, 0.05), 0.245, 0.92,
                                       transform=ax_kpi.transAxes,
                                       facecolor="white", edgecolor=color, linewidth=2))
        ax_kpi.text(x + 0.123, 0.85, heading, transform=ax_kpi.transAxes,
                    ha="center", va="center", fontsize=10, fontweight="bold", color=color)
        ax_kpi.text(x + 0.012, 0.55, body, transform=ax_kpi.transAxes,
                    ha="left", va="center", fontsize=9, fontfamily="monospace")
    # ------- middle row: throughput per rep + drops -------
    ax_t = fig.add_subplot(gs[1, 0])
    rates_per_rep = t.get("rate_actual_per_rep", [])
    err_per_rep = t.get("errors_per_rep", [])
    x_reps = list(range(1, len(rates_per_rep) + 1))
    ax_t.bar(x_reps, rates_per_rep, color="#2c7fb8", label="actual")
    if target:
        ax_t.axhline(target, color="gray", linestyle=":", alpha=0.6, label=f"target {int(target)}")
    ax_t.set_ylim(0, max(target, max(rates_per_rep, default=0)) * 1.15)
    ax_t.set_xlabel("repeat")
    ax_t.set_ylabel("msg/s")
    ax_t.set_title("rate per repeat")
    ax_t.legend(loc="lower right", fontsize=8)
    ax_t.grid(axis="y", alpha=0.3)
    # Drops per rep — derive from cumulative coord metrics-after files.
    ax_d = fig.add_subplot(gs[1, 1])
    drops_per_rep = []
    prev = 0
    for r in range(1, reps + 1):
        m = parse_metrics(run_dir / f"metrics-after-r{r}.txt")
        cur = int(m.get("hermod_messages_dropped_total", 0) or 0)
        drops_per_rep.append(max(0, cur - prev))
        prev = cur
    color = "#d73027" if any(drops_per_rep) else "#1a9850"
    ax_d.bar(x_reps, drops_per_rep, color=color)
    ax_d.set_xlabel("repeat")
    ax_d.set_ylabel("# dropped")
    ax_d.set_title("coord drops per repeat")
    ax_d.grid(axis="y", alpha=0.3)
    if not any(drops_per_rep):
        ax_d.text(0.5, 0.5, "0 drops across all reps",
                  transform=ax_d.transAxes, ha="center", va="center",
                  fontsize=12, color="#1a9850", fontweight="bold")
    # ------- bottom row: latency histogram + resource timeline -------
    ax_h = fig.add_subplot(gs[2, 0])
    last = run_dir / f"metrics-after-r{reps}.txt"
    base = run_dir / "metrics-baseline.txt"
    m_after = parse_metrics(last) if last.exists() else {}
    m_base = parse_metrics(base) if base.exists() else {}
    b_after = m_after.get("hermod_rule_eval_seconds_bucket") or {}
    b_base = m_base.get("hermod_rule_eval_seconds_bucket") or {}
    if b_after:
        edges = sorted(float(le) for le in b_after if le != "+Inf")
        def lookup(d, le):
            for k, v in d.items():
                try:
                    if abs(float(k) - le) < 1e-12: return v
                except ValueError:
                    pass
            return 0
        cum_after = [lookup(b_after, le) for le in edges]
        cum_base = [lookup(b_base, le) for le in edges] if b_base else [0]*len(edges)
        per_after = [cum_after[0]] + [cum_after[i] - cum_after[i-1] for i in range(1, len(cum_after))]
        per_base  = [cum_base[0]]  + [cum_base[i]  - cum_base[i-1]  for i in range(1, len(cum_base))]
        per_bucket = [max(0, a - b) for a, b in zip(per_after, per_base)]
        # bars on log-x
        log_edges = [le * 1000 for le in edges]
        bar_x = []
        bar_w = []
        for i, le in enumerate(log_edges):
            prev = log_edges[i-1] if i > 0 else le * 0.5
            bar_x.append((prev + le) / 2)
            bar_w.append(le - prev)
        ax_h.bar(bar_x, per_bucket, width=bar_w, color="#2c7fb8",
                 edgecolor="white", align="center", alpha=0.85)
        ax_h.set_xscale("log")
        ax_h.set_xlabel("rule_eval latency (ms, log)")
        ax_h.set_ylabel("# msgs in bucket")
        ax_h.set_title("latency histogram")
        for pct_val, label, color in [(lat.get("p50", 0), "p50", "#1a9850"),
                                      (lat.get("p99", 0), "p99", "#fdae61"),
                                      (lat.get("p999", 0), "p99.9", "#d73027")]:
            if pct_val > 0:
                ax_h.axvline(pct_val, color=color, linestyle="--", alpha=0.85,
                             label=f"{label}={pct_val:.2f}ms")
        ax_h.legend(loc="upper right", fontsize=8)
        ax_h.grid(axis="y", alpha=0.3)
    # Resource timeline.
    ax_r = fig.add_subplot(gs[2, 1])
    top = run_dir / "top.tsv"
    if top.exists() and top.stat().st_size:
        series: dict = {}
        rows = top.read_text().splitlines()
        for line in rows[1:]:
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            ts = int(parts[0])
            pod = parts[2]
            cpu = float(re.sub(r"[^\d.]", "", parts[3]) or 0)
            short = re.sub(r"-[a-f0-9]{8,10}-[a-z0-9]{5}$", "", pod)
            v = series.setdefault(short, {"ts": [], "cpu": []})
            v["ts"].append(ts)
            v["cpu"].append(cpu)
        if series:
            t0 = min(min(v["ts"]) for v in series.values())
            for pod, v in sorted(series.items()):
                rel = [t - t0 for t in v["ts"]]
                ax_r.plot(rel, v["cpu"], label=pod, alpha=0.85)
            ax_r.axhline(1500, color="#d73027", linestyle=":", alpha=0.5,
                         label="cpu limit (1500m)")
            ax_r.set_xlabel("seconds since start")
            ax_r.set_ylabel("CPU (millicores)")
            ax_r.set_title("CPU per pod")
            ax_r.legend(fontsize=8, loc="upper right")
            ax_r.grid(alpha=0.3)
    plt.savefig(run_dir / "plot-dashboard.png", dpi=110, bbox_inches="tight")
    plt.close()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"not a directory: {run_dir}", file=sys.stderr)
        return 2
    reps = max(
        (int(p.stem.split("-r")[-1]) for p in run_dir.glob("loadgen-r*.json")),
        default=0,
    )
    if reps == 0:
        return 0
    plot_throughput(run_dir, reps)
    plot_latency(run_dir, reps)
    plot_resources(run_dir)
    write_summary(run_dir, reps)
    plot_dashboard(run_dir, reps)   # last — depends on summary.json
    return 0


if __name__ == "__main__":
    sys.exit(main())
