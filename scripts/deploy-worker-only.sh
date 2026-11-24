#!/usr/bin/env bash
# Use only for engine changes that retain the existing Gateway contract.
set -euo pipefail
exec bash "$(dirname "$0")/update-cloud-run.sh" --worker-only "$@"
