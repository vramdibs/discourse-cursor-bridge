#!/usr/bin/env bash
# Stand-in for cursor-agent during smoke tests.
# Mimics --output-format stream-json: emits one JSON object per line, the
# last one being type=="result" with the answer in `result`.
set -euo pipefail
PROMPT="${*: -1}"
HASH=$(printf %s "$PROMPT" | sha256sum | awk '{print substr($1,1,8)}')
SESSION="smoke-session-$HASH"

printf '{"type":"system","subtype":"init","cwd":"%s","session_id":"%s","model":"smoke","permissionMode":"default"}\n' "$PWD" "$SESSION"
printf '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"<prompt>"}]},"session_id":"%s"}\n' "$SESSION"
printf '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"smoke ok (prompt-hash=%s)"}]},"session_id":"%s"}\n' "$HASH" "$SESSION"
printf '{"type":"result","subtype":"success","duration_ms":1,"is_error":false,"result":"smoke ok (prompt-hash=%s)","session_id":"%s","usage":{"inputTokens":1,"outputTokens":1}}\n' "$HASH" "$SESSION"
