#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <CONFLUENCE_TOKEN> [QUERY]" >&2
  echo "Example: $0 'your_token' 'AI各团队key'" >&2
  exit 1
fi

TOKEN="$1"
QUERY="${2:-AI各团队key}"
MCP_URL="http://10.1.88.121:8087/mcp"

SESSION_ID=$(
  curl -sS -D - -o /dev/null -X POST "$MCP_URL" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/json, text/event-stream" \
    -H "Content-Type: application/json" \
    -d '{
      "jsonrpc": "2.0",
      "id": 1,
      "method": "initialize",
      "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {
          "name": "manual-test",
          "version": "1.0.0"
        }
      }
    }' \
  | tr -d '\r' \
  | awk '/^Mcp-Session-Id:/ {print $2}'
)

if [[ -z "$SESSION_ID" ]]; then
  echo "ERROR: failed to get Mcp-Session-Id" >&2
  exit 2
fi

echo "SESSION_ID=$SESSION_ID" >&2

echo "===== confluence_search: $QUERY =====" >&2
curl -sS -X POST "$MCP_URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "confluence_search",
      "arguments": {
        "query": "'"$QUERY"'"
      }
    }
  }'
