#!/usr/bin/env bash
# Initial installation of one Cloud Run MCP service and one transcription Job.
# Normal updates use update-cloud-run.sh; --reconfigure deliberately reapplies setup.
# Google OAuth is configured in the existing Google Cloud project.
set -euo pipefail

case "${1:-}" in
  "") reconfigure=false ;;
  --reconfigure) reconfigure=true ;;
  *) echo "Usage: $0 [--reconfigure]" >&2; exit 2 ;;
esac
: "${GCP_PROJECT:?Set GCP_PROJECT}"
# Run must be enabled before discovery in a new project.
gcloud services enable run.googleapis.com --project "$GCP_PROJECT"
if ! "$reconfigure"; then
  services="$(gcloud run services list --project "$GCP_PROJECT" --filter='metadata.name=scribeflow-mcp' --format='value(metadata.name)')"
  jobs="$(gcloud run jobs list --project "$GCP_PROJECT" --filter='metadata.name=scribeflow-transcribe' --format='value(metadata.name)')"
  if [[ -n "$services$jobs" ]]; then
    echo "Existing ScribeFlow deployment found. Use scripts/update-cloud-run.sh to preserve its configuration." >&2
    echo "For an intentional migration or incomplete initial setup, supply the full configuration and use --reconfigure." >&2
    exit 1
  fi
fi

for name in SCRIBEFLOW_GOOGLE_CLIENT_ID SCRIBEFLOW_GOOGLE_EMAIL; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing $name (see docs/cloud-run.md)" >&2
    exit 1
  fi
done
REGION="${SCRIBEFLOW_REGION:-asia-northeast3}"
WORKER_REGION="${SCRIBEFLOW_WORKER_REGION:-$REGION}"
JOB=scribeflow-transcribe
SERVICE=scribeflow-mcp
IMAGE="${SCRIBEFLOW_IMAGE:-ghcr.io/ljy0317/scribeflow:sha-$(git rev-parse HEAD)}"
OPENAI_SECRET="${SCRIBEFLOW_OPENAI_SECRET:-scribeflow-openai-api-key}"
GOOGLE_SECRET=scribeflow-google-client-secret
SEED_SECRET=scribeflow-oauth-seed
DRIVE_JOB_SECRET=scribeflow-drive-job-key
ADDITIONAL_EMAILS="${SCRIBEFLOW_GOOGLE_ADDITIONAL_EMAILS:-}"
if [[ "$ADDITIONAL_EMAILS" == *[,=]* ]]; then
  echo "Use a space-separated list of additional Google emails." >&2
  exit 1
fi
WORKER_SA="scribeflow-worker@${GCP_PROJECT}.iam.gserviceaccount.com"
GATEWAY_SA="scribeflow-gateway@${GCP_PROJECT}.iam.gserviceaccount.com"

# Cloud Run deterministic service URL is available before initial deployment.
PROJECT_NUMBER="$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)')"
URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

gcloud services enable secretmanager.googleapis.com \
  firestore.googleapis.com drive.googleapis.com --project "$GCP_PROJECT"

for name in "$OPENAI_SECRET" "$GOOGLE_SECRET"; do
  gcloud secrets versions describe latest --secret="$name" --project="$GCP_PROJECT" >/dev/null
done

if ! gcloud secrets describe "$SEED_SECRET" --project "$GCP_PROJECT" >/dev/null 2>&1; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | \
    gcloud secrets create "$SEED_SECRET" --project "$GCP_PROJECT" \
      --replication-policy=automatic --data-file=- >/dev/null
fi

if ! gcloud secrets describe "$DRIVE_JOB_SECRET" --project "$GCP_PROJECT" >/dev/null 2>&1; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(32) + "=", end="")' |
    gcloud secrets create "$DRIVE_JOB_SECRET" --project "$GCP_PROJECT" \
      --replication-policy=automatic --data-file=- >/dev/null
fi

for name in scribeflow-worker scribeflow-gateway; do
  email="$name@${GCP_PROJECT}.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "$email" --project "$GCP_PROJECT" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$name" --project "$GCP_PROJECT"
done

gcloud secrets add-iam-policy-binding "$OPENAI_SECRET" --project "$GCP_PROJECT" \
  --member="serviceAccount:$WORKER_SA" --role=roles/secretmanager.secretAccessor >/dev/null
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
python3 "$(dirname "$0")/cloud-run-resources.py" retention
gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:$GATEWAY_SA" --role=roles/datastore.user --condition=None >/dev/null
gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:$WORKER_SA" --role=roles/datastore.user \
  --condition="expression=resource.name==\"projects/${GCP_PROJECT}/databases/scribeflow-oauth\",title=scribeflow-worker-results" >/dev/null

for account in "$WORKER_SA" "$GATEWAY_SA"; do
  gcloud secrets add-iam-policy-binding "$DRIVE_JOB_SECRET" --project "$GCP_PROJECT" \
    --member="serviceAccount:$account" --role=roles/secretmanager.secretAccessor >/dev/null
done
worker_secrets="OPENAI_API_KEY=${OPENAI_SECRET}:latest,SCRIBEFLOW_DRIVE_JOB_KEY=${DRIVE_JOB_SECRET}:latest"
gateway_secrets="SCRIBEFLOW_GOOGLE_CLIENT_SECRET=${GOOGLE_SECRET}:latest,SCRIBEFLOW_OAUTH_SEED=${SEED_SECRET}:latest,SCRIBEFLOW_DRIVE_JOB_KEY=${DRIVE_JOB_SECRET}:latest"

worker_env="SCRIBEFLOW_CLOUD_MODE=worker,GOOGLE_CLOUD_PROJECT=${GCP_PROJECT},SCRIBEFLOW_REGION=${WORKER_REGION},SCRIBEFLOW_TRANSCRIPTION_MODEL=${SCRIBEFLOW_TRANSCRIPTION_MODEL:-gpt-transcribe}"
if [[ -n "${OPENAI_BASE_URL:-}" ]]; then
  worker_env+=",OPENAI_BASE_URL=${OPENAI_BASE_URL}"
fi
gcloud run jobs deploy "$JOB" --image "$IMAGE" --project "$GCP_PROJECT" \
  --region "$WORKER_REGION" --service-account "$WORKER_SA" \
  --tasks 1 --parallelism 1 --max-retries 0 --task-timeout 2700s \
  --cpu 1 --memory 2Gi \
  --set-env-vars="$worker_env" \
  --set-secrets="$worker_secrets"

gcloud run jobs add-iam-policy-binding "$JOB" --project "$GCP_PROJECT" \
  --region "$WORKER_REGION" --member="serviceAccount:$GATEWAY_SA" \
  --role=roles/run.jobsExecutorWithOverrides >/dev/null
gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:$GATEWAY_SA" --role=roles/run.viewer --condition=None >/dev/null

# Never leave a previous, unprotected revision publicly invokable.
gcloud run services remove-iam-policy-binding "$SERVICE" --project "$GCP_PROJECT" \
  --region "$REGION" --member=allUsers --role=roles/run.invoker >/dev/null 2>&1 || true

gcloud run deploy "$SERVICE" --image "$IMAGE" --project "$GCP_PROJECT" \
  --region "$REGION" --service-account "$GATEWAY_SA" \
  --cpu 1 --memory 512Mi --min-instances 0 --max-instances 1 \
  --no-allow-unauthenticated \
  --set-env-vars="SCRIBEFLOW_CLOUD_MODE=gateway,GOOGLE_CLOUD_PROJECT=${GCP_PROJECT},SCRIBEFLOW_REGION=${WORKER_REGION},SCRIBEFLOW_JOB=${JOB},SCRIBEFLOW_MCP_URL=${URL},SCRIBEFLOW_GOOGLE_CLIENT_ID=${SCRIBEFLOW_GOOGLE_CLIENT_ID},SCRIBEFLOW_GOOGLE_EMAIL=${SCRIBEFLOW_GOOGLE_EMAIL},SCRIBEFLOW_GOOGLE_ADDITIONAL_EMAILS=${ADDITIONAL_EMAILS}" \
  --set-secrets="$gateway_secrets"

# Cloud Run may return its hash-based alias from status.url, even when the
# deterministic URL registered with Google OAuth is also valid. Do not treat
# differing aliases as a deployment failure.
printf 'OAuth gateway deployed privately at %s/mcp\n' "$URL"
printf 'No audio was transcribed. Run scripts/enable-mcp-access.sh after OAuth setup.\n'
