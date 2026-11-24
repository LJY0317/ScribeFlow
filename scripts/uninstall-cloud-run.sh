#!/usr/bin/env bash
# Preview by default. --apply deletes only ScribeFlow-owned cloud resources.
# Retains the project, user audio/TXT, existing OpenAI/rclone secrets and credentials.
set -euo pipefail
: "${GCP_PROJECT:?Set GCP_PROJECT}"
case "${1:-}" in
  "") apply=false ;;
  --apply) apply=true ;;
  *) echo "Usage: $0 [--apply]" >&2; exit 2 ;;
esac
WORKER="scribeflow-worker@${GCP_PROJECT}.iam.gserviceaccount.com"
GATEWAY="scribeflow-gateway@${GCP_PROJECT}.iam.gserviceaccount.com"
inventory="$(python3 "$(dirname "$0")/cloud-run-resources.py" inventory)"

echo "ScribeFlow removal targets in project $GCP_PROJECT (all regions):"
while IFS='|' read -r kind location name image detail worker_region job; do
  case "$kind" in
    gateway|worker|registry) printf -- '- %s %s (%s)\n' "$kind" "$name" "$location" ;;
    external-worker) printf -- '- External Worker %s (%s): preserved\n' "$name" "$location" ;;
  esac
done <<< "$inventory"
cat <<EOF
Also removes app-owned resources if present:
- Firestore database scribeflow-oauth (including OAuth login state)
- Secret Manager secrets scribeflow-google-client-secret, scribeflow-oauth-seed,
  scribeflow-drive-job-key, and legacy scribeflow-mcp-access-token
- Dedicated service accounts scribeflow-worker and scribeflow-gateway, and their app IAM bindings
Will NOT delete: project, original recordings/transcripts, user-provided OpenAI/rclone secrets.
Separately disconnect the ChatGPT MCP plugin and delete its Google OAuth Web client.
EOF
if ! "$apply"; then
  echo "Dry-run only. To remove all listed ScribeFlow resources, run: bash scripts/uninstall-cloud-run.sh --apply"
  exit 0
fi

# Remove services and jobs before deleting their shared database or credentials.
# Inventory failure or a failed Cloud Run deletion must not strand a live Job.
while IFS='|' read -r kind location name image detail worker_region job; do
  case "$kind" in
    gateway) gcloud run services delete "$name" --project="$GCP_PROJECT" --region="$location" --quiet ;;
    worker) gcloud run jobs delete "$name" --project="$GCP_PROJECT" --region="$location" --quiet ;;
  esac
done <<< "$inventory"
for role in roles/run.viewer roles/datastore.user; do
  gcloud projects remove-iam-policy-binding "$GCP_PROJECT" \
    --member="serviceAccount:$GATEWAY" --role="$role" --condition=None >/dev/null 2>&1 || true
done
gcloud projects remove-iam-policy-binding "$GCP_PROJECT" \
  --member="serviceAccount:$WORKER" --role=roles/datastore.user \
  --condition="expression=resource.name==\"projects/${GCP_PROJECT}/databases/scribeflow-oauth\",title=scribeflow-worker-results" >/dev/null 2>&1 || true
# Revoke this app's access, but preserve the user's OpenAI API key secret.
# An explicit name also supports resuming an earlier partial uninstall.
openai_secrets="$(while IFS='|' read -r kind location name image detail rest; do
  if [[ "$kind" == worker && -n "$detail" ]]; then printf '%s\n' "$detail"; fi
done <<< "$inventory"
printf '%s\n' "${SCRIBEFLOW_OPENAI_SECRET:-scribeflow-openai-api-key}")"
while IFS= read -r secret; do
  if [[ -n "$secret" ]]; then
    gcloud secrets remove-iam-policy-binding "$secret" --project="$GCP_PROJECT" \
      --member="serviceAccount:$WORKER" --role=roles/secretmanager.secretAccessor \
      --condition=None --quiet >/dev/null 2>&1 || true
  fi
done < <(printf '%s\n' "$openai_secrets" | sort -u)
for name in scribeflow-worker scribeflow-gateway; do
  if gcloud iam service-accounts describe "$name@${GCP_PROJECT}.iam.gserviceaccount.com" \
    --project="$GCP_PROJECT" >/dev/null 2>&1; then
    gcloud iam service-accounts delete "$name@${GCP_PROJECT}.iam.gserviceaccount.com" \
      --project="$GCP_PROJECT" --quiet
  fi
done
for name in scribeflow-google-client-secret scribeflow-oauth-seed scribeflow-mcp-access-token scribeflow-drive-job-key; do
  if gcloud secrets describe "$name" --project="$GCP_PROJECT" >/dev/null 2>&1; then
    gcloud secrets delete "$name" --project="$GCP_PROJECT" --quiet
  fi
done
while IFS='|' read -r kind location name rest; do
  if [[ "$kind" == registry ]]; then
    gcloud artifacts repositories delete "$name" --project="$GCP_PROJECT" \
      --location="$location" --quiet
  fi
done <<< "$inventory"
if gcloud firestore databases describe --database=scribeflow-oauth \
  --project="$GCP_PROJECT" >/dev/null 2>&1; then
  gcloud firestore databases delete --database=scribeflow-oauth --project="$GCP_PROJECT" --quiet
fi
printf 'ScribeFlow cloud resources deleted. Original audio and user-provided secrets remain.\n'
