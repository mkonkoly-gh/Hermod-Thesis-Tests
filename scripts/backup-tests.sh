#!/usr/bin/env bash
# Snapshot the entire Tests/ tree to <repo>/Tests-backups/ with a UTC
# timestamp. Excludes archive/ to avoid backing up backups. Idempotent —
# call any time. Hourly cron + ad-hoc both safe.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST_ROOT="${TESTS_BACKUP_DIR:-$(cd "$ROOT/.." && pwd)/Tests-backups}"
mkdir -p "$DEST_ROOT"

TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$DEST_ROOT/tests-${TS}.tar.gz"

# Single tar pass; relative paths so extraction lands cleanly under any
# parent dir. Excludes:
#   archive/      already-archived bad data
#   logs/*tmp*    transient bash mktemp artefacts
#   .venv/        recreatable from the source repo
cd "$(dirname "$ROOT")"
tar -czf "$OUT" \
    --exclude='Tests/archive' \
    --exclude='Tests/.venv' \
    --exclude='Tests/__pycache__' \
    --exclude='Tests/scripts/__pycache__' \
    Tests
SIZE=$(du -h "$OUT" | cut -f1)
echo "wrote $OUT ($SIZE)"

# NO auto-purge. Thesis submission is 2026-05-04 — keep every snapshot
# until v explicitly cleans up post-submission. Disk cost is trivial
# (~2MB per backup, ~50MB/day).
echo "current backups:"
ls -lht "$DEST_ROOT" | head -10
