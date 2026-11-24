#!/usr/bin/env bash
# Read-only inventory across all regions. Does not delete or update anything.
set -euo pipefail
: "${GCP_PROJECT:?Set GCP_PROJECT before inspecting Cloud resources}"
inventory="$(python3 "$(dirname "$0")/cloud-run-resources.py" inventory)"

echo "Deployed ScribeFlow resources (all regions):"
has_registry=false
while IFS='|' read -r kind location name image detail worker_region job; do
  case "$kind" in
    gateway)
      printf 'Gateway %s (%s): %s\n  Ready revision: %s\n  Worker target: %s (%s)\n' \
        "$name" "$location" "$image" "$detail" "$job" "$worker_region"
      ;;
    worker|external-worker)
      printf 'Worker %s (%s): %s\n' "$name" "$location" "$image"
      ;;
    registry)
      has_registry=true
      printf 'Artifact Registry %s (%s)\n' "$name" "$location"
      gcloud artifacts docker images list "${location}-docker.pkg.dev/${GCP_PROJECT}/${name}" \
        --include-tags --project="$GCP_PROJECT" --format=json
      gcloud artifacts repositories list-cleanup-policies "$name" \
        --project="$GCP_PROJECT" --location="$location" --format=json
      ;;
  esac
done <<< "$inventory"
if ! "$has_registry"; then
  echo "No legacy Artifact Registry repository found; standard images are published to GHCR."
fi

echo "Default log bucket retention (if accessible):"
gcloud logging buckets describe _Default \
  --project="$GCP_PROJECT" --location=global --format='yaml(retentionDays)' || true

echo "READ-ONLY AUDIT COMPLETE. No resources changed."
