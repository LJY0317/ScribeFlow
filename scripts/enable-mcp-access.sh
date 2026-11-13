#!/usr/bin/env bash
# Only expose the server once Auth0 has been configured with its real audience.
set -euo pipefail
: "${GCP_PROJECT:?}"
REGION="${SCRIBEFLOW_REGION:-asia-northeast3}"
SERVICE=scribeflow-mcp
URL="$(gcloud run services describe "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --format='value(status.url)')"
if [[ "$URL" != https://* ]]; then
  echo "Invalid Cloud Run URL" >&2
  exit 1
fi

gcloud run services add-iam-policy-binding "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --member=allUsers --role=roles/run.invoker >/dev/null

# Unauthenticated calls must be rejected at the application layer.
response="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --max-time 20 --request POST -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$URL/mcp")" || response="000"
if [[ "$response" != "401" ]]; then
  gcloud run services remove-iam-policy-binding "$SERVICE" --project "$GCP_PROJECT" \
    --region "$REGION" --member=allUsers --role=roles/run.invoker >/dev/null || true
  echo "Security check failed (HTTP $response). Public access rolled back." >&2
  exit 1
fi
printf 'Protected MCP endpoint available: %s/mcp (anonymous requests get HTTP 401)\n' "$URL"
