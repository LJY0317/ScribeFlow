#!/usr/bin/env bash
# Read-only ScribeFlow storage inventory. Does not delete or update anything.
set -euo pipefail

: "${GCP_PROJECT:?Set GCP_PROJECT before inspecting Cloud resources}"
REGION="${SCRIBEFLOW_REGION:-asia-northeast3}"
WORKER_REGION="${SCRIBEFLOW_WORKER_REGION:-$REGION}"
REPO=scribeflow
SERVICE=scribeflow-mcp
JOB=scribeflow-transcribe
REGISTRY="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${REPO}"

command -v gcloud >/dev/null
echo "[1/5] Deployed MCP service (inspect image references)"
gcloud run services describe "$SERVICE" \
  --project="$GCP_PROJECT" --region="$REGION" --format='yaml(status.latestReadyRevisionName,spec.template.spec.containers[0].image)'

echo "[2/5] Deployed transcription Job (inspect image references)"
gcloud run jobs describe "$JOB" \
  --project="$GCP_PROJECT" --region="$WORKER_REGION" --format='yaml(spec.template.spec.template.spec.containers[0].image)'

echo "[3/5] Artifact Registry image inventory"
if gcloud artifacts repositories describe "$REPO" --project="$GCP_PROJECT" --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts docker images list "$REGISTRY" --include-tags \
    --project="$GCP_PROJECT" --format=json
else
  echo "No legacy Artifact Registry repository found; the default image is published to GHCR."
fi

echo "[4/5] Existing Artifact Registry cleanup policies (if any)"
gcloud artifacts repositories list-cleanup-policies "$REPO" \
  --project="$GCP_PROJECT" --location="$REGION" --format=json || true

echo "[5/5] Default log bucket retention (if accessible)"
gcloud logging buckets describe _Default \
  --project="$GCP_PROJECT" --location=global --format=json || true

echo "READ-ONLY AUDIT COMPLETE. No images, logs, audio, transcripts, or claims were removed."
echo "If considering scripts/artifact-cleanup-policy.json, first compare deployed"
echo "image digests to deletion candidates and perform a dry-run review."
