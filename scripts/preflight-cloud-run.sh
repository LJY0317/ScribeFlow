#!/usr/bin/env bash
# Read-only preflight for an ALREADY deployed ScribeFlow instance.
# No Cloud Run deployment, Job execution, TTL changes or image deletion.
set -euo pipefail

: "${GCP_PROJECT:?Set GCP_PROJECT to the existing Cloud project ID}"
REGION="${SCRIBEFLOW_REGION:-asia-northeast3}"
JOB=scribeflow-transcribe
SERVICE=scribeflow-mcp
REPO=scribeflow
OPENAI_SECRET="${SCRIBEFLOW_OPENAI_SECRET:-scribeflow-openai-api-key}"
RCLONE_SECRET="${SCRIBEFLOW_RCLONE_SECRET:-scribeflow-rclone-config}"

for program in git gcloud python3; do
  command -v "$program" >/dev/null || {
    echo "Missing command: $program" >&2
    exit 1
  }
done

if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "Deploy preparation requires main, not another branch." >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Deploy preparation requires a clean git checkout." >&2
  exit 1
fi
LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(GIT_TERMINAL_PROMPT=0 git ls-remote --exit-code origin refs/heads/main | awk 'NR == 1 { print $1 }')"
if [[ -z "$REMOTE_SHA" || "$LOCAL_SHA" != "$REMOTE_SHA" ]]; then
  echo "Cloud Shell checkout differs from GitHub main. Run git pull --ff-only first." >&2
  exit 1
fi

echo "ScribeFlow source: $LOCAL_SHA"
gcloud projects describe "$GCP_PROJECT" --format='value(projectId)' >/dev/null
BILLING="$(gcloud billing projects describe "$GCP_PROJECT" --format='value(billingEnabled)')"
if [[ "${BILLING,,}" != "true" ]]; then
  echo "Billing is not enabled or cannot be verified for $GCP_PROJECT." >&2
  exit 1
fi
echo "Project access and active billing verified."

# These are nonsecret identifiers already registered in Google OAuth.
# The deployed service may contain them, but never print them in a log.
for name in SCRIBEFLOW_GOOGLE_CLIENT_ID SCRIBEFLOW_GOOGLE_EMAIL; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing $name. Restore it from your private existing OAuth configuration." >&2
    exit 1
  fi
done

# Only show image references, never full service JSON (which can contain envs).
for kind in services jobs; do
  resource="$SERVICE"
  if [[ "$kind" == jobs ]]; then resource="$JOB"; fi
  gcloud run "$kind" describe "$resource" \
    --project="$GCP_PROJECT" --region="$REGION" --format=json |
    python3 -c 'import json,sys
data=json.load(sys.stdin)
images=set()
def scan(value):
    if isinstance(value,dict):
        if isinstance(value.get("image"),str):
            images.add(value["image"])
        for nested in value.values():
            scan(nested)
    elif isinstance(value,list):
        for nested in value:
            scan(nested)
scan(data)
if not images:
    sys.exit("Could not identify current deployed image")
print("Existing image(s):",", ".join(sorted(images)))
'
done

gcloud firestore databases describe --database=scribeflow-oauth \
  --project="$GCP_PROJECT" >/dev/null
gcloud artifacts repositories describe "$REPO" \
  --project="$GCP_PROJECT" --location="$REGION" >/dev/null
for secret in "$OPENAI_SECRET" "$RCLONE_SECRET" \
  scribeflow-google-client-secret scribeflow-oauth-seed; do
  gcloud secrets versions describe latest --secret="$secret" \
    --project="$GCP_PROJECT" >/dev/null
done
echo "Existing Firestore database, registry and four secrets verified."

echo "Recent transcription executions (review running jobs before deploying):"
gcloud run jobs executions list --job="$JOB" --limit=5 \
  --project="$GCP_PROJECT" --region="$REGION"

echo "READ-ONLY PREFLIGHT PASSED. No cloud resources were changed."
echo "Run the existing full deployment scripts only when ready:"
echo "  bash scripts/deploy-cloud-run.sh"
echo "  bash scripts/enable-mcp-access.sh"
echo "Then inspect Firestore TTL and artifact retention independently."
