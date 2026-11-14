#!/usr/bin/env bash
# Update only the existing Cloud Run transcription Job image.
# MCP Gateway, Google OAuth, Firestore, IAM, and secrets are untouched.
# Execute manually after a reviewed commit has been merged to main.
set -euo pipefail

: "${GCP_PROJECT:?Set GCP_PROJECT before deploying}"
REGION="${SCRIBEFLOW_REGION:-asia-northeast3}"
JOB=scribeflow-transcribe
REPO=scribeflow

command -v git >/dev/null
command -v gcloud >/dev/null
if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "Refusing worker deployment outside the main branch." >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing worker deployment with uncommitted or untracked changes." >&2
  exit 1
fi

# This command only updates an existing Job; it never creates infrastructure.
gcloud run jobs describe "$JOB" --project="$GCP_PROJECT" \
  --region="$REGION" >/dev/null

COMMIT="$(git rev-parse --short=12 HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${REPO}/scribeflow:${COMMIT}"

echo "Building transcription worker image for commit ${COMMIT}"
gcloud builds submit . --tag "$IMAGE" --project="$GCP_PROJECT"

echo "Updating only Cloud Run Job ${JOB}"
gcloud run jobs update "$JOB" --image "$IMAGE" \
  --project="$GCP_PROJECT" --region="$REGION"
echo "Worker updated: ${IMAGE}"
echo "The MCP Gateway, OAuth, IAM, and secrets were not changed."
echo "No transcription job was executed."
