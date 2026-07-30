#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/run-response-demo.sh [asap-eval demo options]

Print live RAG_ASAP answers, retrieval contexts, and synthetic demonstrations.

Default command:
  uv run asap-eval demo --config config.toml --queries data/rag_tool_demo_queries.json

Examples:
  scripts/run-response-demo.sh
  scripts/run-response-demo.sh --limit 2
  scripts/run-response-demo.sh --question "Из чего состоит двухполюсное митотическое веретено?"

Environment overrides:
  EVAL_CONFIG           Config path, default: config.toml.
  DEMO_QUERIES          Query file, default: data/rag_tool_demo_queries.json.
  COMPONENT_HEALTH_URL  Readiness URL, default: http://localhost:8100/ping.
  SKIP_PREFLIGHT        Set to 1 to skip the readiness check.
  SKIP_UV_SYNC          Set to 1 to skip "uv sync --frozen".
EOF
}

log() {
  printf '[asap-eval:demo] %s\n' "$*"
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
DEMO_QUERIES="${DEMO_QUERIES:-data/rag_tool_demo_queries.json}"
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

if [[ ! -f "$DEMO_QUERIES" ]] && ! has_arg "--question" "$@"; then
  log "ERROR: demo query file was not found: $DEMO_QUERIES"
  exit 1
fi

if [[ "${SKIP_PREFLIGHT:-0}" != "1" ]]; then
  log "Checking component readiness at $COMPONENT_HEALTH_URL"
  curl --fail --silent --show-error "$COMPONENT_HEALTH_URL" >/dev/null
fi

if [[ "${SKIP_UV_SYNC:-0}" != "1" ]]; then
  log "Syncing eval dependencies."
  uv sync --frozen
fi

cmd=(uv run asap-eval demo)
if ! has_arg "--config" "$@"; then
  cmd+=(--config "$EVAL_CONFIG")
fi
if ! has_arg "--queries" "$@" && ! has_arg "--question" "$@"; then
  cmd+=(--queries "$DEMO_QUERIES")
fi
cmd+=("$@")

log "Running: ${cmd[*]}"
exec "${cmd[@]}"
