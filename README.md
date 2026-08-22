# Hermod thesis test campaign

Pi5 8 GB only. The campaign reruns every Pi profile against a clean
test deployment (`hermod-test` namespace with fake-vault42 + NodePort
NanoMQ) so the dataset is internally consistent at submission time.

## Quick start

```sh
# Once the prod session has released the Pi cluster:
scripts/setup-pi-test-env.sh

# Initialise the campaign queue (or resume from existing state)
python3 scripts/orchestrator.py --status     # see what's queued
python3 scripts/orchestrator.py              # walk the queue (blocking ~6.5 h)

# Recommended: launch detached so it survives a terminal close
nohup python3 scripts/orchestrator.py >> logs/orchestrator.log 2>&1 &
```

While it runs, poll progress every 20 min:

```sh
python3 scripts/orchestrator.py --status
tail -f logs/orchestrator.log
ls -la raw/                          # finished run dirs land here
```

Once complete:

```sh
python3 scripts/flake-detect.py     # state/flake-report.md
scripts/promote-to-data.sh          # copy median-throughput run per group into Data/runs/pi5/
python3 ../Data/scripts/build.py --no-copy   # refresh figures
```

## Files

| path | purpose |
| --- | --- |
| `PLAN.md` | human-readable plan, RQ → phase mapping, acceptance gate |
| `plan.yaml` | machine-readable plan; orchestrator hashes this and refuses to walk a divergent queue |
| `profiles/` | frozen Pi profile YAMLs at campaign start |
| `scripts/orchestrator.py` | resumable queue walker; retry + flake state machine |
| `scripts/run-one.sh` | per-run wrapper, attaches per-minute sampler, moves result into `raw/` |
| `scripts/per-minute-sampler.sh` | side-car: node SSH stats + pod describe + coord /proc every 60 s |
| `scripts/setup-pi-test-env.sh` | one-shot: hermod-test namespace + fake-vault42 + NodePort NanoMQ |
| `scripts/flake-detect.py` | post-campaign CV / restart / cache-leak detector |
| `scripts/promote-to-data.sh` | curated subset → `Data/runs/pi5/`, with audit log |
| `state/campaign.json` | orchestrator's source of truth; safe to read, edit only via the orchestrator |
| `state/flake-report.md` | written by flake-detect.py |
| `state/promotion-log.md` | written by promote-to-data.sh |
| `logs/` | per-attempt run-profile-pi.sh logs + orchestrator stdout |
| `raw/` | every run dir from the campaign |
| `promoted/` | the curated subset (mirror of what lands in Data/runs/pi5/) |

## Cron-babysit pattern

The orchestrator is a long-running blocking process. To babysit without
manually polling:

```sh
# /loop or external cron — every 20 min (run from Tests/)
python3 scripts/orchestrator.py --status \
    >> logs/cron-checkin.log
```

The status output is a 12-line summary, not a noisy dump; it's safe to
append to a log every 20 min.

If the orchestrator process dies (host reboot, OOM, kill), starting it
again resumes from the persisted state — any "running" rows on disk
get re-queued automatically.

## Plan-change safety

`state/campaign.json` records `plan_hash = sha256(plan.yaml)` at
campaign start. If you edit `plan.yaml` mid-run, the orchestrator
refuses to walk the existing queue and tells you to either revert or
`--reset --force` (rebuilds the queue from scratch — destroys progress).

## Failure modes the orchestrator catches

- Coordinator pod env doesn't reflect the profile's feature flags
  (the patched `run-profile-pi.sh` exits non-zero with a per-key diff).
- `kubectl rollout status` times out → fail; retry triggers.
- TRUNCATE failure (DB password lookup or psql exec) → fail; retry.
- Rule-seed count mismatch (`SELECT count(*) FROM rules` ≠ `load.devices`).
- 60-min wall-clock timeout per attempt (covers stuck pods / hung loadgen).
- Run dir missing when the runner was supposed to produce one.

After 3 attempts a run is marked `flaky`. After the queue drains, the
orchestrator does ONE final retry of every flaky run, then declares
`completed_at`. The final flake report is generated separately by
`scripts/flake-detect.py`.
