#!/usr/bin/env bash
# Update existing images only; Cloud Run remains the source of configuration.
set -euo pipefail

: "${GCP_PROJECT:?Set GCP_PROJECT to the existing Cloud project ID}"
dry_run=false
worker_only=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=true ;;
    --worker-only) worker_only=true ;;
    *) echo "Usage: $0 [--dry-run] [--worker-only]" >&2; exit 2 ;;
  esac
done

IMAGE="${SCRIBEFLOW_IMAGE:-ghcr.io/ljy0317/scribeflow:sha-$(git rev-parse HEAD)}"
service=scribeflow-mcp
# Read the deployed target using the same resource discovery as audit/uninstall.
target="$(python3 "$(dirname "$0")/cloud-run-resources.py" target)"
read -r gateway_region worker_region job gateway_image <<< "$target"

echo "Existing Gateway: $service ($gateway_region)"
echo "Existing Worker: $job ($worker_region)"
echo "Target image: $IMAGE"
if "$dry_run"; then
  echo "Gateway image: $gateway_image"
  worker_image="$(gcloud run jobs describe "$job" --project="$GCP_PROJECT" --region="$worker_region" \
    --format='value(spec.template.spec.template.spec.containers[0].image)')"
  echo "Worker image: $worker_image"
  echo "Dry run: no cloud resources changed."
  exit 0
fi

gcloud run jobs update "$job" --image "$IMAGE" \
  --project="$GCP_PROJECT" --region="$worker_region"
if ! "$worker_only"; then
  gcloud run services update "$service" --image "$IMAGE" \
    --project="$GCP_PROJECT" --region="$gateway_region"
fi
echo "Image update complete. Existing settings and Secret Manager references retained."
