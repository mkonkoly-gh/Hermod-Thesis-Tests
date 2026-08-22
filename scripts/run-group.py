#!/usr/bin/env python3
"""Run all queued runs for one deployment group.

Each "deployment group" is one of `Tests/profiles-deploy/<NAME>.yaml`.
Apply the YAML, wait rollouts, run every queued/failed run in
`state/campaign.json` whose `overlay == NAME`, with a DB-truncate +
rollout-restart between runs so every run starts from a clean slate.

Usage:
    python3 scripts/run-group.py <NAME>

Examples:
    python3 scripts/run-group.py coord-only
    python3 scripts/run-group.py nanomq-only
"""
from __future__ import annotations

import json
import os
import shutil
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

# Coord+nanomq are the only services that hold campaign state worth
# wiping between runs — postgres data persists by design (clean
# truncate via SQL); translators are stateless.
RESTART_DEPLOYMENTS = {
    "coord-only":      ["hermod-coordinator", "nanomq"],
    "nanomq-only":     ["nanomq"],
    "mosquitto-only":  ["wifi2mqtt"],
    "pgbench-only":    ["postgres"],
    "translator-zigbee": ["zigbee2mqtt", "nanomq"],
    "translator-lora":   ["lora2mqtt", "nanomq"],
    "translator-wifi":   ["wifi2mqtt", "nanomq"],
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


def apply_deployment(group: str) -> str:
    yaml_path = DEPLOY_DIR / f"{group}.yaml"
    if not yaml_path.exists():
        log(f"FATAL: no deployment YAML at {yaml_path}")
        log("Hand-write it before running this group. See coord-only.yaml as a reference.")
        sys.exit(2)
    log(f"applying {yaml_path}")
    kubectl("apply", "-f", str(yaml_path))
    return group  # ns name == group name by convention


def discover_workloads(ns: str) -> tuple[list[str], list[str]]:
    """Return (deployments, statefulsets) actually present in ns. kubectl
    `-o name` returns `deployment.apps/<name>` (not `deployment/<name>`),
    so split on `/` and ignore the resource-group prefix."""
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


def truncate_db(ns: str) -> None:
    """Wipe rule + device + history state from the hermod DB so the next
    run starts fresh. Runs inside the postgres-0 pod via `kubectl exec`.
    Tables that don't exist yet (cold DB) are tolerated via `IF EXISTS`."""
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
    if p.returncode != 0:
        log(f"  WARN: truncate rc={p.returncode}: {p.stderr.strip()[:200]}")


def restart_services(group: str, ns: str) -> None:
    services = RESTART_DEPLOYMENTS.get(group, ["hermod-coordinator"])
    log(f"  rollout restart: {services}")
    for svc in services:
        # Best-effort restart — a service might not be in this deployment.
        p = subprocess.run(
            ["kubectl", "--context", CTX, "-n", ns,
             "rollout", "restart", f"deployment/{svc}"],
            capture_output=True, text=True, check=False,
        )
        if p.returncode != 0:
            log(f"  skip restart {svc}: {p.stderr.strip()[:120]}")
    # Wait everything healthy after the restart.
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


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    group = sys.argv[1]
    ns = apply_deployment(group)
    log("waiting for all workloads to be ready before first run")
    wait_all_ready(ns)
    log("deployment ready")

    state = load_state()
    queue = [r for r in state["runs"] if r.get("overlay") == group
             and r["status"] in ("queued", "failed", "flaky")]
    log(f"queued for {group}: {len(queue)}")
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
            run["status"] = "passed"
            run["result_dir"] = str(result_dir)
            run["error"] = None
            log(f"  PASS {run['id']} -> {result_dir.name}")
        else:
            run["status"] = "failed"
            run["error"] = run.get("error") or f"rc={rc}"
            log(f"  FAIL {run['id']} rc={rc}")
        save_state(state)
        # Between every run: clean DB + restart services + wait healthy.
        truncate_db(ns)
        restart_services(group, ns)
    log(f"group {group} done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
