#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/run-evaluation.sh [asap-eval run options]

Run collection and RAGAS evaluation from self-service-asap-eval.

Default command:
  uv run asap-eval run --config config.toml --max-samples 0

Environment overrides:
  EVAL_CONFIG       Config path, default: config.toml
  MAX_SAMPLES       Sample limit, default: 0. Values <= 0 mean full dataset.
  SKIP_UV_SYNC      Set to 1 to skip "uv sync --frozen".
  SKIP_PREFLIGHT    Set to 1 to skip http://localhost:8100/ping check.
EOF
}

log() {
  printf '[asap-eval:run] %s\n' "$*"
}

has_arg() {
  local name="$1"
  shift
  local arg
  for arg in "$@"; do
    if [[ "$arg" == "$name" || "$arg" == "$name="* ]]; then
      return 0
    fi
  done
  return 1
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
EVAL_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$EVAL_ROOT"

EVAL_CONFIG="${EVAL_CONFIG:-config.toml}"
MAX_SAMPLES="${MAX_SAMPLES-0}"
COMPONENT_HEALTH_URL="${COMPONENT_HEALTH_URL:-http://localhost:8100/ping}"

if [[ ! -f "$EVAL_CONFIG" ]]; then
  if [[ "$EVAL_CONFIG" == "config.toml" && -f config.example.toml ]]; then
    cp config.example.toml config.toml
    log "Created config.toml from config.example.toml."
  else
    log "ERROR: config file was not found: $EVAL_CONFIG"
    exit 1
  fi
fi

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  log "Created .env from .env.example. Fill judge credentials before running a real evaluation."
fi

if [[ "${SKIP_PREFLIGHT:-0}" != "1" ]]; then
  log "Checking component readiness at $COMPONENT_HEALTH_URL"
  curl --fail --silent --show-error "$COMPONENT_HEALTH_URL" >/dev/null
fi

if [[ "${SKIP_UV_SYNC:-0}" != "1" ]]; then
  log "Syncing eval dependencies."
  uv sync --frozen
fi

cmd=(uv run asap-eval run)
if ! has_arg "--config" "$@"; then
  cmd+=(--config "$EVAL_CONFIG")
fi
if [[ -n "$MAX_SAMPLES" && "$MAX_SAMPLES" != "all" ]] && ! has_arg "--max-samples" "$@"; then
  cmd+=(--max-samples "$MAX_SAMPLES")
fi
cmd+=("$@")

log "Running: ${cmd[*]}"
exec "${cmd[@]}"
