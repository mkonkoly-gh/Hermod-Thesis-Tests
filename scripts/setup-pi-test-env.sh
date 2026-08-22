#!/usr/bin/env bash
# Deploy the Pi test environment: hermod-test namespace + fake-vault42 +
# NodePort-exposed NanoMQ. The prod deployment in hermod-prod stays
# untouched so the other session can keep iterating.
#
# This script is IDEMPOTENT — re-applying is safe. It is meant to be run
# once before the first orchestrator pass, then again any time the
# overlay set under kubernetes/overlays/test changes.
#
# Usage:
#   scripts/setup-pi-test-env.sh
#
# Prereqs:
#   - kubectl context pi5-live points at the Pi5
#   - HERMOD_REPO is set or the default below resolves
#
# Implementation note: this script applies the existing dev overlay
# (or a yet-to-be-written test overlay) into a fresh hermod-test
# namespace. If the test overlay does not exist yet, it falls back to
# a copy of overlays/dev with the namespace renamed.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HERMOD_REPO="${HERMOD_REPO:-$REPO_ROOT/Hermod}"
CTX="${HERMOD_KIND_CTX:-pi5-live}"
NS="${HERMOD_PI_NAMESPACE:-hermod-test}"
NODEPORT="${HERMOD_PI_NANOMQ_NODEPORT:-31983}"

log() { printf '\033[1;33m[setup-pi-test-env]\033[0m %s\n' "$*"; }
fatal() { printf '\033[1;31m[setup-pi-test-env]\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Verify kubectl context
kubectl --context "$CTX" get nodes >/dev/null \
    || fatal "cannot reach context '$CTX'"

# 1a. Ensure localhost/fake-vault42:latest exists in the Pi's container store.
# coord-only swaps the vault42 image to this name (RS256 JWKS deterministic,
# no rate-limit flakes); without it, every Coordinator pod start fails on
# ImagePullBackOff. Idempotent — podman build is a no-op if the layers
# haven't changed.
log "ensuring localhost/fake-vault42:latest is built on the Pi node"
PI_USER="${HERMOD_PI_SSH_USER:-ubuntu}"
PI_KEY="${HERMOD_PI_SSH_KEY:-$HOME/.hermod-pi/keys/<pi-host>.key}"
NODE_IP="${HERMOD_PI_NODE_IP:-<pi-ip>}"
if [[ -f "$PI_KEY" ]]; then
    ssh -i "$PI_KEY" -o StrictHostKeyChecking=no "${PI_USER}@${NODE_IP}" \
        "test -d ~/hermod-test/fake-vault42 || rsync -a --delete /mnt/hermod-src/tests/fake-vault42/ ~/hermod-test/fake-vault42/ 2>/dev/null; \
         cd ~/hermod-test/fake-vault42 && podman build -t localhost/fake-vault42:latest . 2>&1 | tail -3" \
        || log "  warn: fake-vault42 build skipped/failed (continuing — operator may have built it manually)"
else
    log "  warn: HERMOD_PI_SSH_KEY not present, skipping fake-vault42 build (assuming already on Pi)"
fi

# 2. Create / ensure namespace
kubectl --context "$CTX" get namespace "$NS" >/dev/null 2>&1 \
    || kubectl --context "$CTX" create namespace "$NS"
log "namespace $NS exists"

# 2a. Seed secrets — base/secrets.yaml is intentionally NOT in any
# kustomization.yaml (so an `apply -k` cannot overwrite real prod
# values with `change-me-*` placeholders). The upstream
# scripts/lib/ensure-secrets.sh populates them imperatively.
# In the test namespace we just want fresh dev-default secrets;
# `defaults` mode is correct here because the test campaign isn't
# exercising secret rotation.
SEED_SCRIPT="$HERMOD_REPO/scripts/lib/ensure-secrets.sh"
if [[ -f "$SEED_SCRIPT" ]]; then
    log "seeding secrets into $NS via ensure-secrets.sh (defaults mode)"
    HERMOD_SECRETS_MODE=defaults \
        HERMOD_NAMESPACE="$NS" \
        HERMOD_KIND_CTX="$CTX" \
        bash -c "source '$SEED_SCRIPT' && ensure_all_secrets" \
        || fatal "secret seeding failed (see ensure-secrets.sh output above)"
else
    fatal "missing $SEED_SCRIPT — cannot seed test secrets"
fi

# 3. Choose overlay
OVERLAY=""
if [[ -d "$HERMOD_REPO/kubernetes/overlays/test-pi" ]]; then
    OVERLAY="$HERMOD_REPO/kubernetes/overlays/test-pi"
elif [[ -d "$HERMOD_REPO/kubernetes/overlays/dev-hardware" ]]; then
    OVERLAY="$HERMOD_REPO/kubernetes/overlays/dev-hardware"
    log "using dev-hardware overlay (no test-pi overlay found yet)"
else
    fatal "no overlay found under $HERMOD_REPO/kubernetes/overlays/"
fi

# 4. Apply overlay into the test namespace.
# kustomize doesn't natively let us override the namespace from the CLI,
# so we render then sed. (kustomize edit set namespace would mutate the
# overlay's kustomization.yaml on disk — undesirable.)
RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT
kubectl --context "$CTX" kustomize "$OVERLAY" > "$RENDERED" \
    || fatal "kustomize render failed"
sed -i "s/namespace: hermod\$/namespace: $NS/g" "$RENDERED"
sed -i "s/namespace: hermod-prod\$/namespace: $NS/g" "$RENDERED"

# 5. Apply
kubectl --context "$CTX" apply -n "$NS" -f "$RENDERED" \
    || fatal "kubectl apply failed"
log "overlay applied to $NS"

# 6. Patch the NanoMQ service to NodePort and pin the port
log "patching nanomq service to NodePort $NODEPORT"
kubectl --context "$CTX" -n "$NS" patch svc nanomq --type=merge -p "{
    \"spec\": {
        \"type\": \"NodePort\",
        \"ports\": [
            {\"name\": \"mqtt\", \"port\": 1883, \"targetPort\": 1883, \"nodePort\": $NODEPORT}
        ]
    }
}" || log "  warn: NodePort patch failed — service may not have a 'mqtt' port; check manually"

# 7. Wait for fake-vault42 + nanomq + coord to be ready
for dep in fake-vault42 nanomq hermod-coordinator postgres; do
    log "waiting for deployment/$dep to roll out"
    kubectl --context "$CTX" -n "$NS" rollout status deployment/"$dep" \
        --timeout=300s 2>/dev/null \
        || kubectl --context "$CTX" -n "$NS" rollout status statefulset/"$dep" \
            --timeout=300s 2>/dev/null \
        || log "  warn: $dep rollout did not converge in 5 min (continuing)"
done

# 8. Smoke-test the broker NodePort from the laptop
log "smoke-testing nanomq via NodePort $NODEPORT"
if command -v mosquitto_pub >/dev/null 2>&1; then
    mosquitto_pub -h "${HERMOD_PI_NODE_IP:-<pi-ip>}" -p "$NODEPORT" \
        -t hermod-test/setup-smoke -m '{"ok":true}' -q 0 \
        && log "  smoke pub OK" \
        || log "  smoke pub FAILED — check NetworkPolicy / firewall"
else
    log "  mosquitto_pub not installed; skipping smoke test"
fi

log "test env ready in namespace $NS, broker NodePort $NODEPORT"
