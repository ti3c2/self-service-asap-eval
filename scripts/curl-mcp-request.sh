#!/usr/bin/env bash
set -Eeuo pipefail

MCP_URL="${MCP_URL:-http://localhost:8100/mcp}"
MCP_URL="${MCP_URL%/}"
MCP_PROTOCOL_VERSION="2025-06-18"
QUERY=""

usage() {
  cat <<'EOF'
Usage: scripts/curl-mcp-request.sh -q <query>

Call the RAG_ASAP MCP tool directly with curl.

Options:
  -q, --query <query>  Query to pass to the MCP tool.
  -h, --help           Show this help.

Environment:
  MCP_URL              MCP endpoint, default: http://localhost:8100/mcp.
EOF
}

die() {
  printf '[asap-eval:curl-mcp] ERROR: %s\n' "$*" >&2
  exit 1
}

json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s' "$value"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -q|--query)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      QUERY="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -n "$QUERY" ]] || die "query is required; pass it with -q or --query"

QUERY_JSON="$(json_escape "$QUERY")"

SESSION_ID="$(
  curl -fisS "$MCP_URL" \
    -H "accept: application/json, text/event-stream" \
    -H "content-type: application/json" \
    --data '{
      "jsonrpc": "2.0",
      "id": "init",
      "method": "initialize",
      "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "curl", "version": "0.1.0"}
      }
    }' \
    | awk 'tolower($1) == "mcp-session-id:" {print $2}' \
    | tr -d '\r'
)"

if [[ -z "$SESSION_ID" ]]; then
  die "MCP initialize did not return a session id from $MCP_URL"
fi

curl -sS "$MCP_URL" \
  -H "accept: application/json, text/event-stream" \
  -H "content-type: application/json" \
  -H "mcp-session-id: $SESSION_ID" \
  -H "mcp-protocol-version: $MCP_PROTOCOL_VERSION" \
  --data '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  >/dev/null

curl -sS --no-buffer "$MCP_URL" \
  -H "accept: application/json, text/event-stream" \
  -H "content-type: application/json" \
  -H "mcp-session-id: $SESSION_ID" \
  -H "mcp-protocol-version: $MCP_PROTOCOL_VERSION" \
  --data '{
    "jsonrpc": "2.0",
    "id": "rag-call",
    "method": "tools/call",
    "params": {
      "name": "RAG_ASAP",
      "arguments": {
        "user_query": "'"$QUERY_JSON"'",
        "return_contexts": true
      }
    }
  }' \
  | awk '
      /^data: / { sub(/^data: /, ""); print; found = 1; next }
      /^[[:space:]]*[{[]/ { print; found = 1 }
      END { if (!found) exit 1 }
    '
