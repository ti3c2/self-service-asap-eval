#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/run-input-contract-test.sh [pytest options]

Run the rag_tool_asap MCP input-contract test from self-service-asap-eval.

Environment overrides:
  ORIG_ROOT             Path to self-service-asap.
  BASE_SERVER_URL       Component base URL, default: http://localhost:8100.
  COMPONENT_HEALTH_URL  Readiness URL, default: $BASE_SERVER_URL/ping.
  SKIP_PREFLIGHT        Set to 1 to skip the readiness check.
EOF
}

log() {
  printf '[asap-eval:input-contract] %s\n' "$*"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EVAL_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ORIG_ROOT="${ORIG_ROOT:-$EVAL_ROOT/../self-service-asap}"

if [[ ! -d "$ORIG_ROOT" ]]; then
  log "ERROR: self-service-asap was not found at $ORIG_ROOT"
  exit 1
fi

ORIG_ROOT="$(cd -- "$ORIG_ROOT" && pwd)"
COMPONENT_DIR="$ORIG_ROOT/services/components/rag_tool_asap"
BASE_SERVER_URL="${BASE_SERVER_URL:-http://localhost:8100}"
BASE_SERVER_URL="${BASE_SERVER_URL%/}"
COMPONENT_HEALTH_URL="${COMPONENT_HEALTH_URL:-$BASE_SERVER_URL/ping}"

if [[ ! -d "$COMPONENT_DIR" ]]; then
  log "ERROR: rag_tool_asap component was not found at $COMPONENT_DIR"
  exit 1
fi

if [[ "${SKIP_PREFLIGHT:-0}" != "1" ]]; then
  log "Checking component readiness at $COMPONENT_HEALTH_URL"
  curl --fail --silent --show-error "$COMPONENT_HEALTH_URL" >/dev/null
fi

cmd=(uv run pytest -s tests/test_rag_tool_input_contract.py)
cmd+=("$@")

log "Running against $BASE_SERVER_URL: ${cmd[*]}"
cd "$COMPONENT_DIR"
BASE_SERVER_URL="$BASE_SERVER_URL" "${cmd[@]}"
