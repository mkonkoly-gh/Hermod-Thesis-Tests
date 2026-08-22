#!/usr/bin/env python3
"""Master driver: walk all deployment groups in order, run each group's
profiles, tear down between groups.

Order: coord-only → nanomq-only → mosquitto-only → pgbench-only →
translator-zigbee → translator-lora → translator-ble.

For each group:
  1. delete previous namespace (if any) — idempotent.
  2. apply Tests/profiles-deploy/<group>.yaml.
  3. wait_all_ready.
  4. for each queued run with overlay==group: run, post-run reset.
  5. delete namespace.

A passed run is never re-attempted. A failed run is left as-is in this
pass; rerun the master to retry. Exits when every overlay's queued/failed
runs are drained or no progress is made.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "campaign.json"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
DEPLOY_DIR = ROOT / "profiles-deploy"
RUN_ONE = ROOT / "scripts" / "run-one.sh"
RAW_DIR = ROOT / "raw"
CTX = "pi5-live"
NODE_IP = "<pi-ip>"

# Order matters: simpler isolation profiles first, then translator stack.
# Each name must match (a) the run's `overlay` field in campaign.json AND
# (b) the YAML filename in profiles-deploy/.
GROUP_ORDER = [
    "coord-only",
    "nanomq-only",
    "mosquitto-only",
    "pgbench-only",
    "translator-zigbee",
    "translator-lora",
    "translator-ble",
]

# Which deployments to rollout-restart between runs in each group. The
# DB always gets truncated; only the listed services get restarted so a
# rule cache / device snapshot from a prior run can't bleed into the next.
RESTART_DEPLOYMENTS = {
    "coord-only":        ["hermod-coordinator", "nanomq"],
    "nanomq-only":       ["nanomq"],
    "mosquitto-only":    ["mosquitto"],
    "pgbench-only":      [],   # postgres is statefulset, restart via separate path
    "translator-zigbee": ["hermod-coordinator", "nanomq", "zigbee2mqtt"],
    "translator-lora":   ["hermod-coordinator", "nanomq", "lora2mqtt"],
    "translator-ble":    ["hermod-coordinator", "nanomq", "omg-ble"],
}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


def kubectl(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--context", CTX, *args]
    if capture:
        return subprocess.run(cmd, check=check, text=True, capture_output=True)
    return subprocess.run(cmd, check=check, text=True)


def wait_rollout(ns: str, kind: str, name: str, timeout: int = 240) -> None:
    log(f"  rollout: {kind}/{name} ({ns}) timeout={timeout}s")
    kubectl("-n", ns, "rollout", "status", f"{kind}/{name}", f"--timeout={timeout}s")


def discover_workloads(ns: str) -> tuple[list[str], list[str]]:
    r = kubectl("-n", ns, "get", "deployments", "-o", "name", capture=True)
    deps = [line.split("/", 1)[1] for line in r.stdout.splitlines() if "/" in line]
    r = kubectl("-n", ns, "get", "statefulsets", "-o", "name", capture=True)
    stss = [line.split("/", 1)[1] for line in r.stdout.splitlines() if "/" in line]
    return deps, stss


def wait_all_ready(ns: str) -> None:
    deps, stss = discover_workloads(ns)
    for d in deps:
        wait_rollout(ns, "deployment", d, timeout=240)
    for s in stss:
        wait_rollout(ns, "statefulset", s, timeout=240)


def apply_deployment(group: str) -> bool:
    yaml_path = DEPLOY_DIR / f"{group}.yaml"
    if not yaml_path.exists():
        log(f"SKIP {group}: no YAML at {yaml_path}")
        return False
    log(f"applying {yaml_path}")
    kubectl("apply", "-f", str(yaml_path))
    return True


def delete_namespace(ns: str) -> None:
    log(f"deleting namespace {ns}")
    kubectl("delete", "ns", ns, "--ignore-not-found", "--wait=true",
            check=False, capture=True)


def has_postgres(ns: str) -> bool:
    r = kubectl("-n", ns, "get", "statefulset", "postgres",
                check=False, capture=True)
    return r.returncode == 0


def truncate_db(ns: str) -> None:
    if not has_postgres(ns):
        return
    sql = """
    DO $$
    DECLARE r record;
    BEGIN
      FOR r IN SELECT tablename FROM pg_tables
               WHERE schemaname='public' AND tablename NOT LIKE '__EFMigrationsHistory%' LOOP
        EXECUTE format('TRUNCATE TABLE %I RESTART IDENTITY CASCADE', r.tablename);
      END LOOP;
    END$$;
    """
    log(f"  truncating hermod tables in {ns}/postgres-0")
    p = subprocess.run(
        ["kubectl", "--context", CTX, "-n", ns,
         "exec", "-c", "postgres", "postgres-0", "--", "psql", "-U", "postgres", "-d", "hermod",
         "-v", "ON_ERROR_STOP=0", "-c", sql],
        capture_output=True, text=True, check=False,
    )
    # truncate failures are not fatal — a cold DB has no tables yet.
    if p.returncode != 0 and "does not exist" not in (p.stderr or ""):
        log(f"  truncate non-fatal rc={p.returncode}: {p.stderr.strip()[:160]}")


def restart_services(group: str, ns: str) -> None:
    services = RESTART_DEPLOYMENTS.get(group, [])
    if not services:
        return
    log(f"  rollout restart: {services}")
    for svc in services:
        p = subprocess.run(
            ["kubectl", "--context", CTX, "-n", ns,
             "rollout", "restart", f"deployment/{svc}"],
            capture_output=True, text=True, check=False,
        )
        if p.returncode != 0:
            log(f"  skip restart {svc}: {p.stderr.strip()[:120]}")
    wait_all_ready(ns)


def load_state() -> dict:
    return json.loads(STATE.read_text())


def save_state(s: dict) -> None:
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(STATE)


def execute_run(run: dict, ns: str) -> tuple[int, Path | None]:
    env = os.environ.copy()
    env["HERMOD_KIND_CTX"] = CTX
    env["HERMOD_PI_NODE_IP"] = NODE_IP
    env["HERMOD_PI_NAMESPACE"] = ns
    env["HERMOD_PI_NANOMQ_NODEPORT"] = "31883"
    env["HERMOD_PI_SKIP_APPLY"] = "1"
    env["HERMOD_AUTH_BYPASS"] = "1"
    env["HERMOD_TOKEN_BASE"] = f"http://{NODE_IP}:42069"
    env["HERMOD_OVERRIDE_REPEATS"] = str(run["reps"])
    if run.get("rate_override") is not None:
        env["HERMOD_OVERRIDE_RATE"] = str(run["rate_override"])
    if run.get("warmup_sec") is not None:
        env["HERMOD_OVERRIDE_WARMUP"] = str(run["warmup_sec"])
    if run.get("measure_sec") is not None:
        env["HERMOD_OVERRIDE_MEASURE"] = str(run["measure_sec"])
    if run.get("cooldown_sec") is not None:
        env["HERMOD_OVERRIDE_COOLDOWN"] = str(run["cooldown_sec"])
    for k, v in (run.get("env") or {}).items():
        env[k] = str(v)
    env["TESTS_RAW_DIR"] = str(RAW_DIR)

    cmd = [str(RUN_ONE), run["profile"], "--tag", run["tag"], "--shape", run["shape"]]
    log_file = LOG_DIR / f"{run['tag']}-attempt{run.get('attempts', 0) + 1}.log"
    log(f"  exec: {' '.join(cmd)}")
    with log_file.open("w") as f:
        f.write(f"# {' '.join(cmd)}\n# env:\n")
        for k, v in env.items():
            if k.startswith(("HERMOD_", "TESTS_")):
                f.write(f"#   {k}={v}\n")
        f.flush()
        proc = subprocess.run(cmd, env=env, stdout=f, stderr=subprocess.STDOUT,
                              check=False, timeout=60 * 60)
    rc = proc.returncode
    result_dir = None
    try:
        last = log_file.read_text().splitlines()[-1].strip()
        if last and Path(last).is_dir():
            result_dir = Path(last)
    except Exception:
        pass
    return rc, result_dir


# Files that MUST be non-empty for a run to count as passing. The
# coord-side artefacts only apply to deployments that actually run a
# Coord pod — broker-only / pgbench-only profiles legitimately have
# no coord metrics, so we filter the required-set per-group below.
REQUIRED_FILES_BY_GROUP = {
    "coord-only":        ["loadgen-r{i}.json", "metrics-baseline.txt", "metrics-before-r{i}.txt", "metrics-after-r{i}.txt"],
    "translator-zigbee": ["loadgen-r{i}.json", "metrics-baseline.txt", "metrics-before-r{i}.txt", "metrics-after-r{i}.txt"],
    "translator-lora":   ["loadgen-r{i}.json", "metrics-baseline.txt", "metrics-before-r{i}.txt", "metrics-after-r{i}.txt"],
    "translator-ble":    ["loadgen-r{i}.json", "metrics-baseline.txt", "metrics-before-r{i}.txt", "metrics-after-r{i}.txt"],
    "nanomq-only":       ["loadgen-r{i}.json"],   # no coord, broker-only
    "mosquitto-only":    ["loadgen-r{i}.json"],   # no coord, broker-only
    "pgbench-only":      [],                      # pgbench writes its own report; runner doesn't drive load_gen
}


def verify_run_quality(group: str, result_dir: Path, reps: int) -> list[str]:
    """Return a list of issue strings; empty list means clean.
    Every required artefact must exist AND be non-zero bytes."""
    issues: list[str] = []
    required = REQUIRED_FILES_BY_GROUP.get(group, [])
    for r in range(1, reps + 1):
        for tmpl in required:
            f = result_dir / tmpl.format(i=r)
            if not f.exists():
                issues.append(f"missing {f.name}")
            elif f.stat().st_size == 0:
                issues.append(f"empty {f.name}")
    return issues


def run_group(group: str) -> int:
    """Drain every queued/failed run for this group. Return: count passed."""
    state = load_state()
    queue = [r for r in state["runs"] if r.get("overlay") == group
             and r["status"] in ("queued", "failed", "flaky")]
    if not queue:
        log(f"group {group}: nothing to run")
        return 0

    if not apply_deployment(group):
        log(f"group {group}: no deployment YAML, marking {len(queue)} runs as no-deploy")
        for run in queue:
            run["status"] = "no-deploy"
        save_state(state)
        return 0

    ns = group
    log(f"group {group}: waiting for workloads")
    wait_all_ready(ns)
    log(f"group {group}: deployment ready, {len(queue)} runs queued")
    passed = 0
    for run in queue:
        run["status"] = "running"
        run["attempts"] = run.get("attempts", 0) + 1
        run["started_at"] = now_iso()
        save_state(state)
        log(f"running {run['id']} attempt={run['attempts']}")
        try:
            rc, result_dir = execute_run(run, ns)
        except subprocess.TimeoutExpired:
            rc, result_dir = 124, None
            run["error"] = "timeout (60 min)"
        run["finished_at"] = now_iso()
        if rc == 0 and result_dir is not None:
            issues = verify_run_quality(group, result_dir, run["reps"])
            if issues:
                run["status"] = "failed"
                run["error"] = "data-quality: " + "; ".join(issues[:5])
                log(f"  FAIL {run['id']} (data quality): {issues[:5]}")
            else:
                run["status"] = "passed"
                run["result_dir"] = str(result_dir)
                run["error"] = None
                passed += 1
                log(f"  PASS {run['id']} -> {result_dir.name}")
                # Per-run plots (best-effort; never fail a passed run on a plot bug).
                try:
                    subprocess.run(
                        ["python3", str(ROOT / "scripts" / "plot-run.py"), str(result_dir)],
                        check=False, capture_output=True, text=True, timeout=60,
                    )
                except Exception as e:
                    log(f"  plot-run.py error: {e}")
        else:
            run["status"] = "failed"
            run["error"] = run.get("error") or f"rc={rc}"
            log(f"  FAIL {run['id']} rc={rc}")
        save_state(state)
        truncate_db(ns)
        restart_services(group, ns)
    return passed


def main() -> int:
    state = load_state()
    if state.get("started_at") is None:
        state["started_at"] = now_iso()
        save_state(state)
    # Resume after a kill: any run still marked "running" was orphaned by
    # the previous driver invocation. Reset to "queued" so this pass picks
    # it up. Without this, a run that was in flight when the driver was
    # killed would be silently skipped forever.
    orphans = [r for r in state["runs"] if r["status"] == "running"]
    for r in orphans:
        log(f"resuming orphaned run {r['id']} (was 'running')")
        r["status"] = "queued"
        r["started_at"] = None
    if orphans:
        save_state(state)

    for group in GROUP_ORDER:
        # Per-group teardown of the PRIOR namespace happens automatically
        # because we only reuse the namespace that matches the group name.
        # If switching groups, kill the previous group's namespace first
        # so its NodePorts free up.
        prev_idx = GROUP_ORDER.index(group) - 1
        if prev_idx >= 0:
            delete_namespace(GROUP_ORDER[prev_idx])
        run_group(group)

    # Close out: if everything is passed, mark the campaign complete.
    state = load_state()
    pending = [r for r in state["runs"] if r["status"] in ("queued", "failed", "flaky")]
    if not pending:
        state["completed_at"] = now_iso()
        save_state(state)
        log(f"campaign complete: {len(state['runs'])} runs all passed/no-deploy")
    else:
        log(f"campaign pass complete; {len(pending)} runs still pending — rerun to retry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
