#!/usr/bin/env python3
"""
Hermod thesis test campaign orchestrator.

Reads plan.yaml, expands every (phase × profile × rate × shape × reps)
into a flat queue persisted at state/campaign.json, and walks it.

Resumable: kill it any time, run it again, it picks up from the persisted
state. Each run gets up to `max_retries` attempts; runs that fail after
all retries are marked `flaky` and retried once more after the rest of
the campaign finishes.

Usage:
    python3 scripts/orchestrator.py            # walk the queue
    python3 scripts/orchestrator.py --status   # print state summary, exit
    python3 scripts/orchestrator.py --reset    # rebuild campaign.json from plan.yaml
                                                 (will refuse if there are non-queued runs unless --force)

The orchestrator does NOT daemonize itself — run it under nohup or in a
detached terminal. The intended babysit pattern is:
    nohup python3 orchestrator.py >> logs/orchestrator.log 2>&1 &
and Claude polling state/campaign.json every 20 minutes via /loop.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PLAN_PATH = ROOT / "plan.yaml"
STATE_PATH = ROOT / "state" / "campaign.json"
PROMOTION_LOG = ROOT / "state" / "promotion-log.md"
RAW_DIR = ROOT / "raw"
LOG_DIR = ROOT / "logs"
RUN_ONE = ROOT / "scripts" / "run-one.sh"

LOG_DIR.mkdir(parents=True, exist_ok=True)
(ROOT / "state").mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    line = f"[{now_iso()}] {msg}"
    print(line, flush=True)


def hash_plan(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def expand_runs(plan: dict) -> list[dict]:
    """Flatten plan.yaml phases into individual run records (one per
    profile × rate × shape combination — reps stay as a single run because
    run-profile-pi.sh handles the rep loop internally)."""
    runs = []

    def add(phase_id: str, overlay: str, tag: str, profile: str,
            rate: int | None, shape: str, reps: int, **extras: object) -> None:
        runs.append({
            "id": tag,
            "phase": phase_id,
            "overlay": overlay,
            "profile": profile,
            "rate_override": rate,
            "shape": shape,
            "reps": reps,
            "warmup_sec": extras.get("warmup_sec"),
            "measure_sec": extras.get("measure_sec"),
            "cooldown_sec": extras.get("cooldown_sec"),
            "env": extras.get("env") or {},
            "tag": tag,
            "status": "queued",
            "attempts": 0,
            "started_at": None,
            "finished_at": None,
            "result_dir": None,
            "error": None,
            "flake_reason": None,
        })

    for phase in plan["phases"]:
        pid = phase["id"]
        phase_overlay = phase.get("overlay", "coord-only")
        # explicit runs list — a run may override the phase overlay.
        for r in phase.get("runs", []):
            add(pid, r.get("overlay", phase_overlay),
                r["tag"], r["profile"],
                r.get("rate"), r.get("shape", "flat"),
                r.get("reps", plan.get("default_reps", 5)),
                warmup_sec=r.get("warmup_sec"),
                measure_sec=r.get("measure_sec"),
                cooldown_sec=r.get("cooldown_sec"),
                env=r.get("env"))
        # profiles × rates expansion
        if "profiles" in phase and "rates" in phase:
            shape = phase.get("shape", "flat")
            reps = phase.get("reps", plan.get("default_reps", 5))
            for prof in phase["profiles"]:
                for rate in phase["rates"]:
                    short = prof.replace("-pi5-live-8gb", "")
                    rate_label = f"{rate // 1000}k" if rate >= 1000 else f"{rate}"
                    tag = f"{pid}-{short}-{rate_label}-{shape}"
                    add(pid, phase_overlay, tag, prof, rate, shape, reps)
    return runs


def apply_overlay(overlay: str, ctx: str, namespace: str) -> int:
    """Apply a scale patch from Tests/scale-patches/<overlay>.yaml against
    the live prod deployment that hermod.sh install brought up. The base
    is the full prod stack; profiles just toggle which workloads are
    running. We track active overlay via a configmap so consecutive runs
    sharing the same overlay don't churn the cluster.

    Patch format (per Tests/scale-patches/coord-only.yaml):
        namespace: hermod-prod
        scale:
            hermod-coordinator: 1
            nanomq: 1
            …
    StatefulSet vs Deployment is auto-detected (postgres is the only
    StatefulSet in the stack). Resources that don't exist are skipped
    with a WARN — ble2mqtt may be commented out in the prod base."""
    patch_path = ROOT / "scale-patches" / f"{overlay}.yaml"
    if not patch_path.exists():
        log(f"  scale-patch missing: {patch_path}")
        return 2
    try:
        patch = yaml.safe_load(patch_path.read_text())
    except yaml.YAMLError as e:
        log(f"  ERROR: scale-patch yaml: {e}")
        return 2
    target_ns = patch.get("namespace") or namespace
    scale = patch.get("scale") or {}
    if not scale:
        log(f"  scale-patch has no scale entries: {patch_path}")
        return 2

    marker = subprocess.run(
        ["kubectl", "--context", ctx, "-n", target_ns, "get", "configmap",
         "test-campaign-state", "-o", "jsonpath={.data.active_overlay}"],
        capture_output=True, text=True, check=False,
    )
    current = marker.stdout.strip() if marker.returncode == 0 else ""
    if current == overlay:
        # Don't trust the marker alone — a pod could have CrashLooped
        # between two same-overlay runs. Verify the expected-up
        # resources are still 1/1; if not, fall through to re-apply.
        all_ready = True
        for resource, replicas in (patch.get("scale") or {}).items():
            if int(replicas) < 1:
                continue
            kind = "statefulset" if resource == "postgres" else "deployment"
            r = subprocess.run(
                ["kubectl", "--context", ctx, "-n", target_ns, "get",
                 kind, resource,
                 "-o", "jsonpath={.status.readyReplicas}/{.spec.replicas}"],
                capture_output=True, text=True, check=False,
            )
            got = r.stdout.strip()
            if r.returncode != 0 or not got.startswith(("1/1", "2/2", "3/3")):
                log(f"  marker says {overlay} but {kind}/{resource} is {got or '<missing>'}; re-applying")
                all_ready = False
                break
        if all_ready:
            log(f"  overlay already active and healthy: {overlay}")
            return 0
    log(f"  applying scale-patch {overlay} (was {current or '<none>'})")

    # Detect kind per resource. Only postgres is a StatefulSet today.
    def kind_for(resource: str) -> str:
        return "statefulset" if resource == "postgres" else "deployment"

    failed = False
    for resource, replicas in scale.items():
        rsc = f"{kind_for(resource)}/{resource}"
        # Check existence first.
        exists = subprocess.run(
            ["kubectl", "--context", ctx, "-n", target_ns, "get", rsc],
            capture_output=True, text=True, check=False,
        )
        if exists.returncode != 0:
            # Hard-fail when an *expected-up* resource is missing — running a
            # profile against a cluster missing vault42 (silent WARN) would
            # produce auth-failed garbage indistinguishable from real flakes.
            # Soft-skip is OK only when we wanted it at 0 and it's already gone.
            if int(replicas) >= 1:
                log(f"  ERROR: {rsc} expected at replicas={replicas} but resource missing in {target_ns}")
                return 4
            log(f"  WARN: {rsc} not present in {target_ns}; skipping (target was 0)")
            continue
        rc = subprocess.run(
            ["kubectl", "--context", ctx, "-n", target_ns, "scale",
             rsc, f"--replicas={int(replicas)}"],
            capture_output=True, text=True, check=False,
        )
        if rc.returncode != 0:
            log(f"  ERROR: scale {rsc} → {replicas} failed: "
                f"{rc.stderr.strip()[:200]}")
            failed = True
        else:
            log(f"    {rsc} → {replicas}")
    if failed:
        return 1

    # Wait for whatever should be ready to actually be ready. Skip the
    # zero-replica entries (rollout status of a 0-replica deployment
    # returns instantly anyway, but we save the kubectl round-trip).
    for resource, replicas in scale.items():
        if int(replicas) == 0:
            continue
        rsc = f"{kind_for(resource)}/{resource}"
        subprocess.run(
            ["kubectl", "--context", ctx, "-n", target_ns, "rollout", "status",
             rsc, "--timeout=300s"],
            check=False,
        )

    # Service-health precheck: every resource we asked to be at >=1
    # replica must actually be Ready 1/1. If anything's degraded right
    # now (CrashLoopBackOff, OOMKilled, stale rollout) we want to fail
    # this overlay-apply so the orchestrator marks the run as failed,
    # the analyzer flags it on the next babysit pass, and we re-run
    # only after the underlying issue is healed. Better than running a
    # profile against a half-deployed cluster and pretending the data
    # is valid.
    expected_up = [r for r, n in scale.items() if int(n) >= 1]
    for resource in expected_up:
        rsc_kind = kind_for(resource)
        # Check ready-replicas vs desired-replicas. For Deployments use
        # .status.readyReplicas; for StatefulSets the same field works.
        ready = subprocess.run(
            ["kubectl", "--context", ctx, "-n", target_ns, "get",
             rsc_kind, resource,
             "-o", "jsonpath={.status.readyReplicas}/{.spec.replicas}"],
            capture_output=True, text=True, check=False,
        )
        if ready.returncode != 0:
            log(f"  WARN: cannot read ready-replicas for {rsc_kind}/{resource}")
            continue
        got = ready.stdout.strip()
        if not got.startswith(("1/1", "2/2", "3/3")):
            # Includes empty / `/<n>` / `0/<n>` shapes — all degraded.
            log(f"  ERROR: {rsc_kind}/{resource} not ready: {got or '<no-status>'}")
            return 3
    # Update marker.
    subprocess.run(
        ["kubectl", "--context", ctx, "-n", target_ns, "apply", "-f", "-"],
        input=(f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
               f"  name: test-campaign-state\n  namespace: {target_ns}\n"
               f"data:\n  active_overlay: {overlay}\n"),
        text=True, capture_output=True, check=False,
    )
    return 0


def init_state(plan: dict, plan_path: Path) -> dict:
    return {
        "campaign_id": now_iso(),
        "plan_hash": hash_plan(plan_path),
        "started_at": now_iso(),
        "completed_at": None,
        "max_retries": plan.get("max_retries", 3),
        "idle_holdoff_sec": plan.get("idle_holdoff_sec", 60),
        "flake_cv_threshold_pct": plan.get("flake_cv_threshold_pct", 5.0),
        "namespace": plan.get("namespace", "hermod-test"),
        "node_ip": plan.get("node_ip", "<pi-ip>"),
        "nanomq_nodeport": plan.get("nanomq_nodeport", 31983),
        "runs": expand_runs(plan),
    }


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_PATH)


def load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    return json.loads(STATE_PATH.read_text())


def status_counts(state: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in state["runs"]:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return out


def print_status(state: dict) -> None:
    c = status_counts(state)
    total = sum(c.values())
    print(f"campaign_id: {state['campaign_id']}")
    print(f"started_at:  {state['started_at']}")
    print(f"completed:   {state.get('completed_at') or '—'}")
    print(f"plan_hash:   {state['plan_hash'][:12]}…")
    print(f"runs:        {total}")
    for s in ("queued", "running", "passed", "failed", "flaky"):
        print(f"  {s:8} {c.get(s, 0)}")
    # show currently running, if any
    running = [r for r in state["runs"] if r["status"] == "running"]
    if running:
        print("\ncurrently running:")
        for r in running:
            print(f"  {r['id']:50}  attempt {r['attempts']}  started {r['started_at']}")
    # last 5 completed
    done = [r for r in state["runs"] if r["status"] in ("passed", "flaky")]
    done.sort(key=lambda r: r.get("finished_at") or "")
    if done:
        print("\nlast 5 completed:")
        for r in done[-5:]:
            print(f"  {r['id']:50}  {r['status']:6}  attempts {r['attempts']}")


def next_queued(state: dict) -> dict | None:
    for r in state["runs"]:
        if r["status"] == "queued":
            return r
    return None


ANALYZER = Path(__file__).resolve().parent / "analyze-run.py"
INCIDENTS = Path(__file__).resolve().parent.parent / "state" / "incidents.md"


def post_run_analysis(run: dict, result_dir: Path) -> tuple[bool, list[str]]:
    """Run analyze-run.py against the result dir. Returns (flagged, flags).

    The orchestrator does NOT pause on flag — autonomous-loop philosophy
    is: continue churning the queue, leave the flag visible for the
    babysit loop to react to. We just record the flag list onto the run
    record and append a one-liner to state/incidents.md so the babysit
    loop has a single file to scan.
    """
    if not ANALYZER.exists():
        return False, []
    proc = subprocess.run(
        ["python3", str(ANALYZER), str(result_dir)],
        capture_output=True, text=True, check=False, timeout=120,
    )
    summary_path = result_dir / "analysis" / "summary.json"
    flags: list[str] = []
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text())
            flags = summary.get("flags", []) or []
        except json.JSONDecodeError:
            flags = ["analyzer-output-malformed"]
    flagged = bool(flags) or proc.returncode == 1
    if flagged:
        INCIDENTS.parent.mkdir(parents=True, exist_ok=True)
        with INCIDENTS.open("a") as fh:
            fh.write(
                f"- [{now_iso()}] **{run['id']}** — flags: {', '.join(flags) or 'unknown'}; "
                f"analysis: `{(result_dir / 'analysis').as_posix()}`; status: unhandled\n"
            )
    return flagged, flags


def execute_run(run: dict, state: dict) -> tuple[int, Path | None]:
    """Spawn run-one.sh, capture exit code + result_dir."""
    env = os.environ.copy()
    env["HERMOD_KIND_CTX"] = "pi5-live"
    env["HERMOD_PI_NODE_IP"] = state["node_ip"]
    env["HERMOD_PI_NANOMQ_NODEPORT"] = str(state["nanomq_nodeport"])
    env["HERMOD_PI_NAMESPACE"] = state["namespace"]
    # The orchestrator owns deployment via apply_overlay() on
    # Tests/kubernetes/overlays/<overlay>. Don't let the runner re-apply
    # its own (incompatible) overlay on top — it'd race on NodePorts and
    # smash configmaps. Runner becomes a thin "drive load_gen" wrapper.
    env["HERMOD_PI_SKIP_APPLY"] = "1"
    # Coord + LoRa2MQTT run with Hermod__Security__AuthBypass=true; test
    # scripts skip the vault42 token round-trip when this flag is set.
    env["HERMOD_AUTH_BYPASS"] = "1"
    env["HERMOD_TOKEN_BASE"] = f"http://{state['node_ip']}:42069"
    env["HERMOD_OVERRIDE_REPEATS"] = str(run["reps"])
    if run["rate_override"] is not None:
        env["HERMOD_OVERRIDE_RATE"] = str(run["rate_override"])
    if run["warmup_sec"] is not None:
        env["HERMOD_OVERRIDE_WARMUP"] = str(run["warmup_sec"])
    if run["measure_sec"] is not None:
        env["HERMOD_OVERRIDE_MEASURE"] = str(run["measure_sec"])
    if run["cooldown_sec"] is not None:
        env["HERMOD_OVERRIDE_COOLDOWN"] = str(run["cooldown_sec"])
    for k, v in (run.get("env") or {}).items():
        env[k] = str(v)
    env["TESTS_RAW_DIR"] = str(RAW_DIR)

    cmd = [str(RUN_ONE), run["profile"], "--tag", run["tag"], "--shape", run["shape"]]
    log(f"  exec: {' '.join(cmd)}")
    log_file = LOG_DIR / f"{run['tag']}-attempt{run['attempts']+1}.log"
    with log_file.open("w") as f:
        f.write(f"# {' '.join(cmd)}\n# env overrides:\n")
        for k, v in env.items():
            if k.startswith(("HERMOD_", "TESTS_")):
                f.write(f"#   {k}={v}\n")
        f.flush()
        proc = subprocess.run(cmd, env=env, stdout=f, stderr=subprocess.STDOUT,
                              check=False, timeout=60 * 60)
    rc = proc.returncode
    # The run-profile-pi.sh script echoes the result_dir on its last line.
    result_dir = None
    try:
        last = log_file.read_text().splitlines()[-1].strip()
        if last and Path(last).is_dir():
            result_dir = Path(last)
    except Exception:
        pass
    return rc, result_dir


def walk_queue(state: dict) -> None:
    save_state(state)
    while True:
        run = next_queued(state)
        if run is None:
            break
        # Switch overlay if needed before marking running.
        overlay = run.get("overlay", "coord-only")
        rc_overlay = apply_overlay(overlay, "pi5-live", state["namespace"])
        if rc_overlay != 0:
            run["status"] = "failed"
            run["error"] = f"overlay switch failed (rc={rc_overlay})"
            run["attempts"] += 1
            if run["attempts"] >= state["max_retries"]:
                run["status"] = "flaky"
                run["flake_reason"] = run["error"]
            else:
                run["status"] = "queued"   # retry next cycle
            save_state(state)
            time.sleep(state["idle_holdoff_sec"])
            continue
        run["status"] = "running"
        run["started_at"] = now_iso()
        run["attempts"] += 1
        save_state(state)
        log(f"running {run['id']}  attempt {run['attempts']}/{state['max_retries']}  overlay={overlay}")
        try:
            rc, result_dir = execute_run(run, state)
        except subprocess.TimeoutExpired:
            rc, result_dir = 124, None
            run["error"] = "timeout (60 min)"
        run["finished_at"] = now_iso()
        if rc == 0 and result_dir is not None:
            run["status"] = "passed"
            run["result_dir"] = str(result_dir)
            run["error"] = None
            log(f"  PASS  {run['id']}  -> {result_dir.name}")
            # Analyse synchronously; flags are recorded but the queue
            # walk does NOT halt on them. The babysit loop is responsible
            # for picking up unhandled flags and acting (auto-rerun /
            # auto-fix / escalate).
            try:
                flagged, flags = post_run_analysis(run, result_dir)
                run["analysis_flags"] = flags
                run["analysis_flagged"] = flagged
                if flagged:
                    log(f"  FLAGGED {run['id']}  flags={','.join(flags)} (continuing; babysit loop handles)")
            except Exception as e:
                log(f"  WARN analyzer crashed for {run['id']}: {type(e).__name__}: {e}")
        else:
            log(f"  FAIL  {run['id']}  rc={rc}  attempts={run['attempts']}")
            if run["attempts"] < state["max_retries"]:
                run["status"] = "queued"     # retry
                run["error"] = f"rc={rc} (will retry)"
            else:
                run["status"] = "flaky"
                run["flake_reason"] = f"failed all {state['max_retries']} attempts (last rc={rc})"
                run["error"] = run["flake_reason"]
        save_state(state)
        time.sleep(state["idle_holdoff_sec"])

    # End-of-campaign: one more pass over flaky runs after a 5-min idle
    flaky = [r for r in state["runs"] if r["status"] == "flaky"]
    if flaky:
        log(f"all queued done; cool-off 300s, then re-running {len(flaky)} flaky")
        time.sleep(300)
        for run in flaky:
            run["status"] = "running"
            run["started_at"] = now_iso()
            run["attempts"] += 1
            save_state(state)
            log(f"reattempt-flaky {run['id']}  attempt {run['attempts']}")
            rc, result_dir = execute_run(run, state)
            run["finished_at"] = now_iso()
            if rc == 0 and result_dir is not None:
                run["status"] = "passed"
                run["result_dir"] = str(result_dir)
                run["flake_reason"] = None
                run["error"] = None
                log(f"  RECOVERED  {run['id']}")
            else:
                run["status"] = "flaky"
                run["flake_reason"] = f"flaky after final retry (rc={rc})"
                log(f"  STILL-FLAKY  {run['id']}")
            save_state(state)

    state["completed_at"] = now_iso()
    save_state(state)
    log("campaign complete")


def _matches(run: dict, *, run_id: str | None, phase: str | None,
             status: str | None, since: str | None) -> bool:
    """Helper for the various --rerun-* flag selectors."""
    if run_id is not None and run["id"] == run_id:
        return True
    if phase is not None and run["phase"] == phase:
        return True
    if status is not None and run["status"] == status:
        return True
    if since is not None:
        f = run.get("finished_at")
        if f and f >= since:
            return True
    return False


def requeue_runs(state: dict, *, run_id: str | None = None,
                 phase: str | None = None, status: str | None = None,
                 since: str | None = None) -> int:
    """Move matching runs back to status=queued so the walker picks them up
    on the next pass. Preserves the previous result_dir + finished_at +
    error in `previous_attempts` so the rerun history stays auditable. The
    new attempt naturally lands in a fresh result dir because run-one.sh
    stamps a new timestamp into RUN_ID."""
    n = 0
    for run in state["runs"]:
        if not _matches(run, run_id=run_id, phase=phase,
                        status=status, since=since):
            continue
        if run["status"] == "queued":
            continue   # already queued
        prev = run.setdefault("previous_attempts", [])
        prev.append({
            "rerun_count": len(prev),
            "status": run["status"],
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "result_dir": run.get("result_dir"),
            "error": run.get("error"),
            "flake_reason": run.get("flake_reason"),
            "attempts": run.get("attempts", 0),
        })
        run["status"] = "queued"
        run["attempts"] = 0
        run["started_at"] = None
        run["finished_at"] = None
        run["result_dir"] = None
        run["error"] = None
        run["flake_reason"] = None
        # Clear the per-attempt analyzer state too — the new attempt
        # will produce its own analysis. The previous_attempts[] entry
        # we just appended preserves the old flags for audit.
        run["analysis_flagged"] = False
        run["analysis_flags"] = []
        run.pop("babysit_handled_at", None)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true",
                    help="print state summary and exit")
    ap.add_argument("--reset", action="store_true",
                    help="rebuild campaign.json from plan.yaml (refuses to overwrite "
                         "in-progress state unless --force)")
    ap.add_argument("--force", action="store_true",
                    help="allow --reset over a non-empty in-progress state")
    ap.add_argument("--rerun", metavar="RUN_ID",
                    help="re-queue a single run by tag (e.g. B-no-cache-2k-flat)")
    ap.add_argument("--rerun-phase", metavar="PHASE",
                    help="re-queue every run in a given phase (e.g. B)")
    ap.add_argument("--rerun-status", metavar="STATUS",
                    help="re-queue every run currently in <status> (passed|failed|flaky|running)")
    ap.add_argument("--rerun-since", metavar="ISO_TS",
                    help="re-queue every run that finished at or after the given UTC ISO timestamp "
                         "(e.g. 2026-04-27T03:00:00Z)")
    ap.add_argument("--list-flagged", action="store_true",
                    help="print every run with analysis_flagged=true and "
                         "no recorded babysit handling, then exit. Used by "
                         "the autonomous babysit loop.")
    ap.add_argument("--mark-handled", metavar="RUN_ID",
                    help="record that the babysit loop has handled the named "
                         "flagged run (so it does not get reacted to twice).")
    args = ap.parse_args()

    plan = load_yaml(PLAN_PATH)
    state = load_state()

    if args.reset:
        if state is not None and not args.force:
            cnts = status_counts(state)
            non_queued = sum(v for k, v in cnts.items() if k != "queued")
            if non_queued > 0:
                log(f"refusing --reset: {non_queued} runs already non-queued. Use --force.")
                return 2
        state = init_state(plan, PLAN_PATH)
        save_state(state)
        log(f"campaign reset; {len(state['runs'])} runs queued")
        return 0

    if state is None:
        state = init_state(plan, PLAN_PATH)
        save_state(state)
        log(f"new campaign; {len(state['runs'])} runs queued")
    else:
        # Refuse to walk if the plan changed under us
        if state["plan_hash"] != hash_plan(PLAN_PATH):
            log("ABORT: plan.yaml hash differs from campaign.json. "
                "Either revert plan.yaml or run --reset --force.")
            return 3

    if args.list_flagged:
        unhandled = [
            r for r in state["runs"]
            if r.get("analysis_flagged") and not r.get("babysit_handled_at")
        ]
        for r in unhandled:
            print(f"{r['id']}\t{r['phase']}\t{','.join(r.get('analysis_flags') or [])}\t"
                  f"reruns={len(r.get('previous_attempts') or [])}\t"
                  f"result_dir={r.get('result_dir', '')}")
        return 0

    if args.mark_handled:
        for r in state["runs"]:
            if r["id"] == args.mark_handled:
                r["babysit_handled_at"] = now_iso()
                r.setdefault("babysit_actions", []).append(now_iso())
                save_state(state)
                log(f"marked {r['id']} as babysit-handled")
                return 0
        log(f"no run with id={args.mark_handled}")
        return 1

    rerun_args = (args.rerun, args.rerun_phase, args.rerun_status, args.rerun_since)
    if any(rerun_args):
        n = requeue_runs(
            state,
            run_id=args.rerun,
            phase=args.rerun_phase,
            status=args.rerun_status,
            since=args.rerun_since,
        )
        if n == 0:
            log("no matching runs to re-queue")
            return 1
        save_state(state)
        log(f"re-queued {n} run(s); previous result dirs preserved in state.runs[].previous_attempts")
        return 0

    if args.status:
        print_status(state)
        return 0

    # Defensive: any "running" rows on resume need to be re-queued so a
    # crash mid-run doesn't leave a phantom row.
    requeued = 0
    for r in state["runs"]:
        if r["status"] == "running":
            r["status"] = "queued"
            requeued += 1
    if requeued:
        log(f"requeued {requeued} runs found stuck in 'running' (likely crash recovery)")
        save_state(state)

    walk_queue(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
