#!/usr/bin/env bash
# Enable Cloud Run HTTP invocation only after both wrong-path and secret-path checks.
# Keep the resulting complete URL private: it acts as an access key.
set -euo pipefail
: "${GCP_PROJECT:?}"
REGION="${SCRIBEFLOW_REGION:-asia-northeast3}"
SERVICE=scribeflow-mcp
MCP_SECRET=scribeflow-mcp-access-token
URL="$(gcloud run services describe "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --format='value(status.url)')"
if [[ "$URL" != https://* ]]; then
  echo 'Cloud Run service HTTPS URL not found' >&2
  exit 1
fi

TOKEN="$(gcloud secrets versions access latest --project="$GCP_PROJECT" --secret="$MCP_SECRET")"
if [[ ! "$TOKEN" =~ ^[A-Za-z0-9_-]{40,128}$ ]]; then
  echo 'MCP URL secret missing or invalid' >&2
  exit 1
fi

gcloud run services add-iam-policy-binding "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --member=allUsers --role=roles/run.invoker >/dev/null

payload='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"scribe-healthcheck","version":"1.0"}}}'
bad="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 \
  -X POST -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d "$payload" "$URL/mcp" || true)"
good="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 \
  -X POST -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d "$payload" "$URL/$TOKEN/mcp" || true)"
if [[ "$bad" != 404 || "$good" != 200 ]]; then
  gcloud run services remove-iam-policy-binding "$SERVICE" --project "$GCP_PROJECT" \
    --region "$REGION" --member=allUsers --role=roles/run.invoker >/dev/null || true
  echo "Security check failed (ordinary path $bad, secret path $good); public access rolled back." >&2
  exit 1
fi

printf 'Verified: ordinary MCP URL returns 404; private URL returns 200.\n'
printf 'Copy this full URL into ChatGPT Plugins > Add custom MCP server > No authentication:\n%s/%s/mcp\n' "$URL" "$TOKEN"
unset TOKEN
printf 'Keep the URL private; it grants access to paid transcription requests.\n'
