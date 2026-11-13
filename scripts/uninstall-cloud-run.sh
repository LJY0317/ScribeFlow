#!/usr/bin/env bash
# Preview by default. --apply deletes only ScribeFlow-owned cloud resources.
# Retains the project, user audio/TXT, existing OpenAI/rclone secrets and credentials.
set -euo pipefail
: "${GCP_PROJECT:?Set GCP_PROJECT}"
REGION="${SCRIBEFLOW_REGION:-asia-northeast3}"
WORKER="scribeflow-worker@${GCP_PROJECT}.iam.gserviceaccount.com"
GATEWAY="scribeflow-gateway@${GCP_PROJECT}.iam.gserviceaccount.com"
OPENAI_SECRET="${SCRIBEFLOW_OPENAI_SECRET:-scribeflow-openai-api-key}"
RCLONE_SECRET="${SCRIBEFLOW_RCLONE_SECRET:-scribeflow-rclone-config}"

if [[ "${1:-}" != '--apply' ]]; then
  cat <<EOF
Dry-run only. To apply, run: bash scripts/uninstall-cloud-run.sh --apply
Will delete from project $GCP_PROJECT:
- Cloud Run service scribeflow-mcp; job scribeflow-transcribe
- Firestore database scribeflow-oauth (including OAuth login state)
- Artifact Registry repository scribeflow (including its images)
- Secret Manager secrets scribeflow-google-client-secret and scribeflow-oauth-seed
- Dedicated service accounts scribeflow-worker and scribeflow-gateway
Will NOT delete: project, original recordings/TXT, existing OpenAI/rclone secrets.
Separately disconnect the ChatGPT MCP plugin and delete its Google OAuth Web client
at Google Cloud Console > Google Auth Platform > Clients.
EOF
  exit 0
fi

gcloud run services remove-iam-policy-binding scribeflow-mcp --project="$GCP_PROJECT" \
  --region="$REGION" --member=allUsers --role=roles/run.invoker >/dev/null 2>&1 || true
for kind in services jobs; do
  resource=scribeflow-mcp
  if [[ "$kind" == jobs ]]; then resource=scribeflow-transcribe; fi
  if gcloud run "$kind" describe "$resource" --project="$GCP_PROJECT" \
    --region="$REGION" >/dev/null 2>&1; then
    gcloud run "$kind" delete "$resource" --project="$GCP_PROJECT" \
      --region="$REGION" --quiet
  fi
done
for role in roles/run.viewer roles/datastore.user; do
  gcloud projects remove-iam-policy-binding "$GCP_PROJECT" \
    --member="serviceAccount:$GATEWAY" --role="$role" >/dev/null 2>&1 || true
done
for name in "$OPENAI_SECRET" "$RCLONE_SECRET"; do
  gcloud secrets remove-iam-policy-binding "$name" --project="$GCP_PROJECT" \
    --member="serviceAccount:$WORKER" \
    --role=roles/secretmanager.secretAccessor >/dev/null 2>&1 || true
done
for name in scribeflow-worker scribeflow-gateway; do
  if gcloud iam service-accounts describe "$name@${GCP_PROJECT}.iam.gserviceaccount.com" \
    --project="$GCP_PROJECT" >/dev/null 2>&1; then
    gcloud iam service-accounts delete "$name@${GCP_PROJECT}.iam.gserviceaccount.com" \
      --project="$GCP_PROJECT" --quiet
  fi
done
for name in scribeflow-google-client-secret scribeflow-oauth-seed scribeflow-mcp-access-token; do
  if gcloud secrets describe "$name" --project="$GCP_PROJECT" >/dev/null 2>&1; then
    gcloud secrets delete "$name" --project="$GCP_PROJECT" --quiet
  fi
done
if gcloud artifacts repositories describe scribeflow --project="$GCP_PROJECT" \
  --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories delete scribeflow --project="$GCP_PROJECT" \
    --location="$REGION" --quiet
fi
if gcloud firestore databases describe --database=scribeflow-oauth \
  --project="$GCP_PROJECT" >/dev/null 2>&1; then
  gcloud firestore databases delete --database=scribeflow-oauth --project="$GCP_PROJECT" --quiet
fi
printf 'ScribeFlow cloud resources deleted. Original audio and user-provided secrets remain.\n'
