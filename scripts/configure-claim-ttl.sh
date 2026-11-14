#!/usr/bin/env bash
# Enable 7-day metadata retention for ScribeFlow-only Firestore job claims.
# This does not touch OAuth tokens, source audio, finished transcripts, or jobs.
set -euo pipefail

: "${GCP_PROJECT:?Set GCP_PROJECT before configuring Firestore retention}"
DATABASE=scribeflow-oauth
COLLECTION=scribeflow_job_claims
FIELD=expires_at

gcloud firestore fields ttls update "$FIELD" \
  --collection-group="$COLLECTION" \
  --database="$DATABASE" \
  --project="$GCP_PROJECT" \
  --enable-ttl

echo "Enabled TTL for $DATABASE/$COLLECTION.$FIELD; records expire 7 days after claim creation."
echo "Firestore TTL deletion is asynchronous and may incur small deletion charges."
