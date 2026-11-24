#!/usr/bin/env bash
# Preview the existing image-only deployment without changing cloud resources.
set -euo pipefail
exec bash "$(dirname "$0")/update-cloud-run.sh" --dry-run "$@"
