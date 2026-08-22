#!/usr/bin/env bash
# Archive run dirs to Tests/archive/<ts>-<reason>.tar.gz instead of
# deleting them. Recoverable by `tar -xzf <archive>` into raw/.
#
# Usage:
#   archive-runs.sh <reason-tag> [path-glob]
#
# Examples:
#   archive-runs.sh pre-reset                        # archives raw/2026*
#   archive-runs.sh bad-1000m-baseline 'raw/*A-baseline-*'
#
# If a glob is omitted, defaults to raw/2026*.
# Always preserves originals — caller is responsible for rm AFTER
# verifying archive integrity.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null && pwd)"
ARCHIVE_DIR="$ROOT/archive"
mkdir -p "$ARCHIVE_DIR"

REASON="${1:?usage: archive-runs.sh <reason-tag> [path-glob]}"
shift
GLOB="${1:-raw/2026*}"

cd "$ROOT"
shopt -s nullglob
matches=( $GLOB )
if (( ${#matches[@]} == 0 )); then
    echo "no matches for: $GLOB"
    exit 0
fi
TS=$(date -u +%Y%m%dT%H%M%SZ)
SAFE_REASON=$(echo "$REASON" | tr ' /' '__')
OUT="$ARCHIVE_DIR/${TS}-${SAFE_REASON}.tar.gz"

echo "archiving ${#matches[@]} dirs/files → $OUT"
tar -czf "$OUT" "${matches[@]}"
SIZE=$(du -h "$OUT" | cut -f1)
echo "wrote $OUT ($SIZE)"
echo
echo "to recover: cd $ROOT && tar -xzf $OUT"
