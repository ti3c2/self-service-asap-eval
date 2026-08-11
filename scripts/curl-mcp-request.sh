#!/usr/bin/bash

MCP_URL="${MCP_URL:-http://localhost:8100/mcp}"
MCP_PROTOCOL_VERSION="2025-06-18"

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

if [ -z "$SESSION_ID" ]; then
  echo "MCP initialize did not return a session id. Check that MCP_URL has no trailing slash: $MCP_URL" >&2
  exit 1
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
        "user_query": "Из чего состоит двухполюсное митотическое веретено?",
        "return_contexts": true
      }
    }
  }' \
  | awk '
      /^data: / { sub(/^data: /, ""); print; found = 1; next }
      /^[[:space:]]*[{[]/ { print; found = 1 }
      END { if (!found) exit 1 }
    '