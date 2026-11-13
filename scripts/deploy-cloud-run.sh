#!/usr/bin/env bash
# Run from Google Cloud Shell. Deploys no public endpoint and does not run a transcription.
set -euo pipefail

: "${GCP_PROJECT:?Set GCP_PROJECT to the existing project ID}"
REGION="${SCRIBEFLOW_REGION:-asia-northeast3}"
JOB=scribeflow-transcribe
SERVICE=scribeflow-mcp
REPO=scribeflow
OPENAI_SECRET="${SCRIBEFLOW_OPENAI_SECRET:-scribeflow-openai-api-key}"
RCLONE_SECRET="${SCRIBEFLOW_RCLONE_SECRET:-scribeflow-rclone-config}"
MCP_SECRET=scribeflow-mcp-access-token
WORKER_SA="scribeflow-worker@${GCP_PROJECT}.iam.gserviceaccount.com"
GATEWAY_SA="scribeflow-gateway@${GCP_PROJECT}.iam.gserviceaccount.com"

gcloud config set project "$GCP_PROJECT"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com --project "$GCP_PROJECT"

for name in "$OPENAI_SECRET" "$RCLONE_SECRET"; do
  gcloud secrets versions describe latest --secret="$name" --project "$GCP_PROJECT" >/dev/null
done

# Generate a secret URL suffix without showing it in command output or history.
if ! gcloud secrets describe "$MCP_SECRET" --project "$GCP_PROJECT" >/dev/null 2>&1; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(36), end="")' | \
    gcloud secrets create "$MCP_SECRET" --project "$GCP_PROJECT" \
      --replication-policy=automatic --data-file=- >/dev/null
fi
gcloud secrets versions describe latest --secret="$MCP_SECRET" \
  --project="$GCP_PROJECT" >/dev/null

for name in scribeflow-worker scribeflow-gateway; do
  email="$name@${GCP_PROJECT}.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "$email" --project "$GCP_PROJECT" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$name" --project "$GCP_PROJECT"
done

for name in "$OPENAI_SECRET" "$RCLONE_SECRET"; do
  gcloud secrets add-iam-policy-binding "$name" --project "$GCP_PROJECT" \
    --member="serviceAccount:$WORKER_SA" --role=roles/secretmanager.secretAccessor >/dev/null
done
gcloud secrets add-iam-policy-binding "$MCP_SECRET" --project "$GCP_PROJECT" \
  --member="serviceAccount:$GATEWAY_SA" \
  --role=roles/secretmanager.secretAccessor >/dev/null

if ! gcloud artifacts repositories describe "$REPO" --location "$REGION" \
  --project "$GCP_PROJECT" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPO" --repository-format=docker \
    --location "$REGION" --project "$GCP_PROJECT" \
    --description="ScribeFlow Cloud Run images"
fi

IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${REPO}/scribeflow:$(git rev-parse --short HEAD)"
gcloud builds submit . --tag "$IMAGE" --project "$GCP_PROJECT"

gcloud run jobs deploy "$JOB" --image "$IMAGE" --project "$GCP_PROJECT" \
  --region "$REGION" --service-account "$WORKER_SA" \
  --tasks 1 --parallelism 1 --max-retries 0 --task-timeout 3600s \
  --cpu 1 --memory 2Gi \
  --set-env-vars="SCRIBEFLOW_CLOUD_MODE=worker,SCRIBEFLOW_RCLONE_CONFIG_SECRET=/secrets/rclone.conf" \
  --set-secrets="/secrets/rclone.conf=${RCLONE_SECRET}:latest,OPENAI_API_KEY=${OPENAI_SECRET}:latest"

gcloud run jobs add-iam-policy-binding "$JOB" --project "$GCP_PROJECT" \
  --region "$REGION" --member="serviceAccount:$GATEWAY_SA" \
  --role=roles/run.jobsExecutorWithOverrides >/dev/null
gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:$GATEWAY_SA" --role=roles/run.viewer >/dev/null

gcloud run deploy "$SERVICE" --image "$IMAGE" --project "$GCP_PROJECT" \
  --region "$REGION" --service-account "$GATEWAY_SA" \
  --cpu 1 --memory 512Mi --min-instances 0 --max-instances 1 \
  --no-allow-unauthenticated \
  --set-env-vars="SCRIBEFLOW_CLOUD_MODE=gateway,GOOGLE_CLOUD_PROJECT=${GCP_PROJECT},SCRIBEFLOW_REGION=${REGION},SCRIBEFLOW_JOB=${JOB}" \
  --set-secrets="SCRIBEFLOW_MCP_ACCESS_TOKEN=${MCP_SECRET}:latest"

# If redeploying an older service, explicitly remove any prior public permission.
gcloud run services remove-iam-policy-binding "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --member=allUsers --role=roles/run.invoker >/dev/null 2>&1 || true

URL="$(gcloud run services describe "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --format='value(status.url)')"
printf 'Private ScribeFlow MCP deployed at %s\n' "$URL"
printf 'No transcription was executed. Next: bash scripts/enable-mcp-access.sh\n'
