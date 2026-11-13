#!/usr/bin/env bash
# OAuth metadata and unauthenticated requests are tested before keeping public access.
set -euo pipefail
: "${GCP_PROJECT:?}"
REGION="${SCRIBEFLOW_REGION:-asia-northeast3}"
SERVICE=scribeflow-mcp
URL="$(gcloud run services describe "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --format='value(status.url)')"
if [[ "$URL" != https://* ]]; then
  echo 'Cloud Run HTTPS URL not found' >&2
  exit 1
fi

gcloud run services add-iam-policy-binding "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --member=allUsers --role=roles/run.invoker >/dev/null

code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --max-time 30 --request POST -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$URL/mcp" || true)"
metadata="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --max-time 30 "$URL/.well-known/oauth-protected-resource/mcp" || true)"
if [[ "$code" != 401 || "$metadata" != 200 ]]; then
  gcloud run services remove-iam-policy-binding "$SERVICE" --project "$GCP_PROJECT" \
    --region "$REGION" --member=allUsers --role=roles/run.invoker >/dev/null || true
  echo "OAuth check failed (anonymous MCP=$code, metadata=$metadata). Gateway reverted to private." >&2
  exit 1
fi
printf 'OAuth gateway is ready at %s/mcp\n' "$URL"
printf 'In ChatGPT, add one custom MCP connection to this URL and select OAuth login.\n'
