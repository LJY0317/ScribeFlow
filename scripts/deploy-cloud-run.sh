#!/usr/bin/env bash
# Run from a Google Cloud Shell with an authenticated, billing-enabled project.
# Secrets must already exist in Secret Manager; never put credentials in git.
set -euo pipefail

for var in GCP_PROJECT SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID SCRIBEFLOW_OAUTH_ISSUER SCRIBEFLOW_ALLOWED_SUBJECT; do
  if [[ -z "${!var:-}" ]]; then
    printf 'Missing %s. See docs/cloud-run.md\n' "$var" >&2
    exit 1
  fi
done

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud not installed. Run this script from Google Cloud Shell." >&2
  exit 1
fi

REGION="${SCRIBEFLOW_REGION:-asia-northeast3}"
JOB="scribeflow-transcribe"
SERVICE="scribeflow-mcp"
WORKER_SA="scribeflow-worker@${GCP_PROJECT}.iam.gserviceaccount.com"
GATEWAY_SA="scribeflow-gateway@${GCP_PROJECT}.iam.gserviceaccount.com"

gcloud config set project "$GCP_PROJECT"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com --project "$GCP_PROJECT"

for name in scribeflow-openai-api-key scribeflow-rclone-config; do
  gcloud secrets describe "$name" --project "$GCP_PROJECT" >/dev/null
done

for name in scribeflow-worker scribeflow-gateway; do
  email="$name@${GCP_PROJECT}.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "$email" --project "$GCP_PROJECT" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$name" --project "$GCP_PROJECT"
done

for name in scribeflow-openai-api-key scribeflow-rclone-config; do
  gcloud secrets add-iam-policy-binding "$name" --project "$GCP_PROJECT" \
    --member "serviceAccount:$WORKER_SA" --role roles/secretmanager.secretAccessor >/dev/null
done

gcloud run jobs deploy "$JOB" --source . --project "$GCP_PROJECT" --region "$REGION" \
  --service-account "$WORKER_SA" --tasks 1 --parallelism 1 \
  --max-retries 0 --task-timeout 3600s --cpu 1 --memory 2Gi \
  --set-env-vars="SCRIBEFLOW_CLOUD_MODE=worker,SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID=${SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID},SCRIBEFLOW_RCLONE_CONFIG_SECRET=/secrets/rclone.conf" \
  --set-secrets="/secrets/rclone.conf=scribeflow-rclone-config:latest,OPENAI_API_KEY=scribeflow-openai-api-key:latest"

gcloud run jobs add-iam-policy-binding "$JOB" --project "$GCP_PROJECT" --region "$REGION" \
  --member="serviceAccount:$GATEWAY_SA" \
  --role=roles/run.jobsExecutorWithOverrides >/dev/null
gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:$GATEWAY_SA" --role=roles/run.viewer >/dev/null

# The first deployment is IAM-private. A placeholder URL is safe because no
# valid JWT can target that audience. Discover the real HTTPS URL afterwards.
gcloud run deploy "$SERVICE" --source . --project "$GCP_PROJECT" --region "$REGION" \
  --service-account "$GATEWAY_SA" --cpu 1 --memory 512Mi \
  --min-instances 0 --max-instances 1 --no-allow-unauthenticated \
  --set-env-vars="SCRIBEFLOW_CLOUD_MODE=gateway,GOOGLE_CLOUD_PROJECT=${GCP_PROJECT},SCRIBEFLOW_REGION=${REGION},SCRIBEFLOW_JOB=${JOB},SCRIBEFLOW_MCP_URL=https://placeholder.invalid,SCRIBEFLOW_OAUTH_ISSUER=${SCRIBEFLOW_OAUTH_ISSUER},SCRIBEFLOW_ALLOWED_SUBJECT=${SCRIBEFLOW_ALLOWED_SUBJECT}"

URL="$(gcloud run services describe "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --format='value(status.url)')"
if [[ "$URL" != https://* ]]; then
  echo "No Cloud Run service HTTPS URL returned. Service remains private." >&2
  exit 1
fi
gcloud run services update "$SERVICE" --project "$GCP_PROJECT" --region "$REGION" \
  --update-env-vars="SCRIBEFLOW_MCP_URL=$URL"

echo "Cloud Run service deployed privately at $URL"
echo "Configure Auth0 MCP API audience to: $URL/mcp"
echo "After confirming the OAuth issuer, audience, allowed subject, and DCR settings,"
echo "run the separate scripts/enable-mcp-access.sh security check."
