#!/usr/bin/env bash
# Send a fake post_created webhook to a locally running bridge.
# Useful to test the full pipeline without poking real Discourse.
#
# Usage:
#   scripts/smoke-webhook.sh <topic_id> <post_id> <post_number> "<raw text>"
# Env:
#   BRIDGE_URL    default http://127.0.0.1:8787/discourse/webhook
#   WEBHOOK_SECRET   if set, request will be signed with sha256
#   USERNAME      default admin
set -euo pipefail

TOPIC_ID="${1:?topic_id required}"
POST_ID="${2:?post_id required}"
POST_NUMBER="${3:?post_number required}"
RAW="${4:?raw text required}"

BRIDGE_URL="${BRIDGE_URL:-http://127.0.0.1:8787/discourse/webhook}"
USERNAME="${USERNAME:-admin}"

PAYLOAD=$(jq -nc \
  --argjson tid "$TOPIC_ID" \
  --argjson pid "$POST_ID" \
  --argjson pn "$POST_NUMBER" \
  --arg user "$USERNAME" \
  --arg raw "$RAW" \
  '{post: {id: $pid, topic_id: $tid, post_number: $pn, username: $user, raw: $raw}}')

HEADERS=(
  -H "Content-Type: application/json"
  -H "X-Discourse-Event-Type: post"
  -H "X-Discourse-Event: post_created"
)

if [ -n "${WEBHOOK_SECRET:-}" ]; then
  SIG="sha256=$(printf %s "$PAYLOAD" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | awk '{print $2}')"
  HEADERS+=( -H "X-Discourse-Event-Signature: $SIG" )
fi

curl -sS -X POST "${HEADERS[@]}" --data "$PAYLOAD" "$BRIDGE_URL"
echo
