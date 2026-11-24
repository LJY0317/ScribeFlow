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
list_args=(--project="$GCP_PROJECT" --filter="metadata.name=$service" --format=json)
if [[ -n "${SCRIBEFLOW_REGION:-}" ]]; then
  list_args+=(--region="$SCRIBEFLOW_REGION")
fi

# Locate the existing Gateway, then use its actual Job routing configuration.
# Only the selected resource names, regions and image leave the JSON parser.
target="$(gcloud run services list "${list_args[@]}" |
  python3 -c '
import json, sys
services = [s for s in json.load(sys.stdin) if s["metadata"]["name"] == "scribeflow-mcp"]
if len(services) != 1:
    sys.exit("Expected one existing scribeflow-mcp Gateway. For multiple regions, select SCRIBEFLOW_REGION; for a new installation, use deploy-cloud-run.sh.")
s = services[0]
region = s["metadata"]["labels"]["cloud.googleapis.com/location"]
container = s["spec"]["template"]["spec"]["containers"][0]
env = {e["name"]: e.get("value") for e in container.get("env", [])}
worker_region = env.get("SCRIBEFLOW_REGION", "asia-northeast3")
job = env.get("SCRIBEFLOW_JOB", "scribeflow-transcribe")
if not worker_region or not job:
    sys.exit("The existing Gateway Job configuration is incomplete.")
print(region, worker_region, job, container["image"])
')"
read -r gateway_region worker_region job gateway_image <<< "$target"
if [[ -n "${SCRIBEFLOW_WORKER_REGION:-}" && "$SCRIBEFLOW_WORKER_REGION" != "$worker_region" ]]; then
  echo "SCRIBEFLOW_WORKER_REGION differs from the existing Gateway target ($worker_region). Change Job routing explicitly before updating." >&2
  exit 1
fi

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
