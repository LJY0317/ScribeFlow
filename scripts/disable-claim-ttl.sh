#!/usr/bin/env bash
# Disable the legacy paid TTL after deploying request-time claim cleanup.
# This does not touch OAuth tokens, source audio, finished transcripts, or jobs.
set -euo pipefail

: "${GCP_PROJECT:?Set GCP_PROJECT before disabling the old Firestore TTL}"
DATABASE=scribeflow-oauth
COLLECTION=scribeflow_job_claims
FIELD=expires_at

gcloud firestore fields ttls update "$FIELD" \
  --collection-group="$COLLECTION" \
  --database="$DATABASE" \
  --project="$GCP_PROJECT" \
  --disable-ttl

echo "Disabled legacy TTL for $DATABASE/$COLLECTION.$FIELD."
