#!/usr/bin/env bash
# One Google Cloud Run MCP service + one on-demand transcription Job.
# Google OAuth is configured in the existing Google Cloud project.
set -euo pipefail

for name in GCP_PROJECT SCRIBEFLOW_GOOGLE_CLIENT_ID SCRIBEFLOW_GOOGLE_EMAIL; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing $name (see docs/cloud-run.md)" >&2
    exit 1
  fi
done
REGION="${SCRIBEFLOW_REGION:-asia-northeast3}"
JOB=scribeflow-transcribe
SERVICE=scribeflow-mcp
REPO=scribeflow
OPENAI_SECRET="${SCRIBEFLOW_OPENAI_SECRET:-scribeflow-openai-api-key}"
RCLONE_SECRET="${SCRIBEFLOW_RCLONE_SECRET:-scribeflow-rclone-config}"
GOOGLE_SECRET=scribeflow-google-client-secret
SEED_SECRET=scribeflow-oauth-seed
WORKER_SA="scribeflow-worker@${GCP_PROJECT}.iam.gserviceaccount.com"
GATEWAY_SA="scribeflow-gateway@${GCP_PROJECT}.iam.gserviceaccount.com"

# Cloud Run deterministic service URL is available before initial deployment.
PROJECT_NUMBER="$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)')"
URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

gcloud config set project "$GCP_PROJECT"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  firestore.googleapis.com --project "$GCP_PROJECT"

for name in "$OPENAI_SECRET" "$RCLONE_SECRET" "$GOOGLE_SECRET"; do
  gcloud secrets versions describe latest --secret="$name" --project="$GCP_PROJECT" >/dev/null
done

if ! gcloud secrets describe "$SEED_SECRET" --project "$GCP_PROJECT" >/dev/null 2>&1; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | \
    gcloud secrets create "$SEED_SECRET" --project "$GCP_PROJECT" \
      --replication-policy=automatic --data-file=- >/dev/null
fi

for name in scribeflow-worker scribeflow-gateway; do
  email="$name@${GCP_PROJECT}.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "$email" --project "$GCP_PROJECT" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$name" --project "$GCP_PROJECT"
done

for name in "$OPENAI_SECRET" "$RCLONE_SECRET"; do
  gcloud secrets add-iam-policy-binding "$name" --project "$GCP_PROJECT" \
    --member="serviceAccount:$WORKER_SA" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done
for name in "$GOOGLE_SECRET" "$SEED_SECRET"; do
  gcloud secrets add-iam-policy-binding "$name" --project "$GCP_PROJECT" \
    --member="serviceAccount:$GATEWAY_SA" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done

# A separate, application-owned database makes uninstall safe and complete.
if ! gcloud firestore databases describe --database=scribeflow-oauth \
  --project="$GCP_PROJECT" >/dev/null 2>&1; then
  gcloud firestore databases create --database=scribeflow-oauth \
    --location="$REGION" --type=firestore-native --project="$GCP_PROJECT"
fi
gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:$GATEWAY_SA" --role=roles/datastore.user >/dev/null

if ! gcloud artifacts repositories describe "$REPO" --location "$REGION" \
  --project "$GCP_PROJECT" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPO" --repository-format=docker \
    --location "$REGION" --project "$GCP_PROJECT"
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

# Never leave a previous, unprotected revision publicly invokable.
gcloud run services remove-iam-policy-binding "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --member=allUsers --role=roles/run.invoker >/dev/null 2>&1 || true

gcloud run deploy "$SERVICE" --image "$IMAGE" --project "$GCP_PROJECT" \
  --region "$REGION" --service-account "$GATEWAY_SA" \
  --cpu 1 --memory 512Mi --min-instances 0 --max-instances 1 \
  --no-allow-unauthenticated \
  --set-env-vars="SCRIBEFLOW_CLOUD_MODE=gateway,GOOGLE_CLOUD_PROJECT=${GCP_PROJECT},SCRIBEFLOW_REGION=${REGION},SCRIBEFLOW_JOB=${JOB},SCRIBEFLOW_MCP_URL=${URL},SCRIBEFLOW_GOOGLE_CLIENT_ID=${SCRIBEFLOW_GOOGLE_CLIENT_ID},SCRIBEFLOW_GOOGLE_EMAIL=${SCRIBEFLOW_GOOGLE_EMAIL}" \
  --set-secrets="SCRIBEFLOW_GOOGLE_CLIENT_SECRET=${GOOGLE_SECRET}:latest,SCRIBEFLOW_OAUTH_SEED=${SEED_SECRET}:latest"

ACTUAL_URL="$(gcloud run services describe "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --format='value(status.url)')"
if [[ "$ACTUAL_URL" != "$URL" ]]; then
  echo "Service is private. Expected $URL, actual $ACTUAL_URL; update Google OAuth redirect configuration before publishing." >&2
  exit 1
fi
printf 'OAuth gateway deployed privately at %s/mcp\n' "$URL"
printf 'No audio was transcribed. Run scripts/enable-mcp-access.sh after OAuth setup.\n'
