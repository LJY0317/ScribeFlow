"""Fail-closed, cross-instance protection against duplicate paid Cloud Run Jobs.

A claim is scoped to a Google Drive file ID (not its filename or keywords).
We reuse the gateway's existing Firestore database and authorized session.
Firestore stores only the SHA-256 of the file ID and a Cloud Run operation ID;
it never stores audio, transcripts, keywords, or OAuth tokens.

Firestore's atomic createDocument and updateTime preconditions provide safe
claim/retry races even across distinct Cloud Run gateway instances.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .cloud_run import _FILE_ID, CloudRunJobs, normalize_transcription_context

COLLECTION = "scribeflow_job_claims"
# A Cloud Run task times out after 1 hour; an unknown submission must not be
# retried automatically, including after this safety window.
UNKNOWN_SUBMISSION_RETRY_SECONDS = 2 * 60 * 60
CLAIM_RETENTION_DAYS = 7


def _starting_document(now: int) -> dict:
    # TTL is deliberately much longer than the Cloud Run task's one-hour limit.
    expires = (
        datetime.fromtimestamp(now, tz=timezone.utc)
        + timedelta(days=CLAIM_RETENTION_DAYS)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {"fields": {
        "started_at": {"integerValue": str(now)},
        "operation": {"stringValue": ""},
        "expires_at": {"timestampValue": expires},
    }}


def _recorded_operation(document: dict) -> str:
    return document.get("fields", {}).get("operation", {}).get("stringValue", "")


class FirestoreJobClaims:
    """Small Firestore REST adapter, using existing google-auth credentials."""

    def __init__(
        self, project: str, session: Any, database: str = "scribeflow-oauth",
    ) -> None:
        self.session = session
        self.collection_url = (
            f"https://firestore.googleapis.com/v1/projects/{project}/"
            f"databases/{database}/documents/{COLLECTION}"
        )

    def _document_url(self, key: str) -> str:
        return self.collection_url + "/" + key

    def create(self, key: str, now: int) -> dict | None:
        response = self.session.request(
            "POST", self.collection_url,
            params={"documentId": key}, json=_starting_document(now),
            timeout=20,
        )
        if response.status_code == 409:  # atomic already-exists conflict
            return None
        response.raise_for_status()  # fail closed; no paid job
        return response.json()

    def read(self, key: str) -> dict | None:
        response = self.session.request(
            "GET", self._document_url(key), timeout=20,
        )
        if response.status_code == 404:  # a competing writer removed the claim
            return None
        response.raise_for_status()
        return response.json()

    def reset(self, key: str, previous: dict, now: int) -> dict | None:
        """Compare-and-swap an explicitly retried failed or stale claim."""
        response = self.session.request(
            "PATCH", self._document_url(key),
            params={"currentDocument.updateTime": previous["updateTime"]},
            json=_starting_document(now), timeout=20,
        )
        if response.status_code in (404, 409, 412):
            return None  # lost race; another caller owns this claim
        response.raise_for_status()
        return response.json()

    def publish(self, key: str, claim: dict, operation: str) -> None:
        """Save the Cloud Run operation using the exact claimed version."""
        fields = claim["fields"]
        response = self.session.request(
            "PATCH", self._document_url(key),
            params={"currentDocument.updateTime": claim["updateTime"]},
            json={"fields": {
                "started_at": fields["started_at"],
                "operation": {"stringValue": operation},
                "expires_at": fields["expires_at"],
            }},
            timeout=20,
        )
        response.raise_for_status()


class DeduplicatingCloudRunJobs:
    """Submit at most one job per source until an explicit terminal/stale retry.

    Ambiguous Cloud Run request errors deliberately leave a reservation behind
    instead of issuing a second possibly billable request.
    """

    def __init__(
        self, backend: CloudRunJobs, claims: FirestoreJobClaims,
        clock: Any = time.time,
    ) -> None:
        self.backend = backend
        self.claims = claims
        self.clock = clock

    def status(self, operation: str) -> dict:
        return self.backend.status(operation)

    def _submit(
        self, key: str, claim: dict, file_id: str,
        keywords: tuple[str, ...], prompt: str,
    ) -> dict:
        # If Cloud Run accepted this request but its response was lost, the
        # claim stays pending. This is intentionally not auto-retried.
        result = self.backend.start(file_id, keywords=keywords, prompt=prompt)
        try:
            self.claims.publish(key, claim, result["operation"])
        except Exception:
            return {
                **result,
                "warning": (
                    "Job started but its duplicate-protection record could "
                    "not be confirmed. Do not submit again; check this operation."
                ),
            }
        return result

    def start(
        self, file_id: str, keywords: list[str] | tuple[str, ...] | None = None,
        prompt: str = "", retry: bool = False,
    ) -> dict:
        if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
            raise ValueError("A valid Google Drive file ID is required")
        normalized, prompt = normalize_transcription_context(keywords, prompt)
        if not isinstance(retry, bool):
            raise ValueError("retry must be a boolean")

        key = hashlib.sha256(file_id.encode("utf-8")).hexdigest()
        for _ in range(4):
            now = int(self.clock())
            claim = self.claims.create(key, now)
            if claim is not None:
                return self._submit(key, claim, file_id, normalized, prompt)

            existing = self.claims.read(key)
            if existing is None:
                continue  # raced a removal; attempt the atomic create again

            operation = _recorded_operation(existing)
            if operation:
                try:
                    current = self.backend.status(operation)
                except Exception:
                    # An operation may eventually expire from the Cloud Run API.
                    # A deliberate retry after the safety window is possible,
                    # but never automatically triggered.
                    try:
                        age = now - int(
                            existing["fields"]["started_at"]["integerValue"]
                        )
                    except (KeyError, TypeError, ValueError):
                        age = 0
                    if retry and age >= UNKNOWN_SUBMISSION_RETRY_SECONDS:
                        claim = self.claims.reset(key, existing, now)
                        if claim is not None:
                            return self._submit(
                                key, claim, file_id, normalized, prompt
                            )
                        continue
                    return {
                        "status": "operation_unavailable",
                        "operation": operation,
                        "duplicate": True,
                        "retry_requires_confirmation": True,
                    }
                state = current.get("status")
                if state in ("failed", "completed") and retry:
                    claim = self.claims.reset(key, existing, now)
                    if claim is not None:
                        return self._submit(key, claim, file_id, normalized, prompt)
                    continue
                return {
                    "status": (
                        "previous_failed" if state == "failed"
                        else "already_completed" if state == "completed"
                        else "already_running"
                    ),
                    "operation": operation,
                    "duplicate": True,
                }

            # A previous request may have launched a job but lost the response
            # before publishing the operation. Never auto-release this claim.
            try:
                age = now - int(existing["fields"]["started_at"]["integerValue"])
            except (KeyError, TypeError, ValueError):
                age = 0  # invalid data fails closed
            if retry and age >= UNKNOWN_SUBMISSION_RETRY_SECONDS:
                claim = self.claims.reset(key, existing, now)
                if claim is not None:
                    return self._submit(key, claim, file_id, normalized, prompt)
                continue
            return {
                "status": (
                    "submission_unconfirmed"
                    if age >= UNKNOWN_SUBMISSION_RETRY_SECONDS
                    else "already_submitting"
                ),
                "duplicate": True,
                "retry_requires_confirmation": True,
            }

        # High contention is safer to reject than to start another paid job.
        return {"status": "already_submitting", "duplicate": True}
