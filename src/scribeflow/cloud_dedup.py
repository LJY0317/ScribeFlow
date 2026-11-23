"""Fail-closed, cross-instance protection against duplicate paid Cloud Run Jobs.

A claim is scoped to a Google Drive file ID (not its filename or keywords).
We reuse the gateway's existing Firestore database and authorized session.
Firestore stores the hashed file ID, operation, timestamps, and the requesting
profile ID in linked mode. The claim contains no OAuth credentials.

Firestore's atomic createDocument and updateTime preconditions provide safe
claim/retry races even across distinct Cloud Run gateway instances.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .cloud_run import _FILE_ID, CloudRunJobs, normalize_language, normalize_transcription_context

COLLECTION = "scribeflow_job_claims"
_CLEANUP_MARKER = "cleanup_day"
_CLEANUP_LIMIT = 100
logger = logging.getLogger(__name__)
# A Cloud Run task times out after 1 hour; an unknown submission must not be
# retried automatically, including after this safety window.
UNKNOWN_SUBMISSION_RETRY_SECONDS = 2 * 60 * 60
CLAIM_RETENTION_DAYS = 7


def _starting_document(now: int, owner: str = "") -> dict:
    # The retention period is much longer than the Cloud Run task's one-hour limit.
    expires = (
        datetime.fromtimestamp(now, tz=timezone.utc)
        + timedelta(days=CLAIM_RETENTION_DAYS)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {"fields": {
        "started_at": {"integerValue": str(now)},
        "operation": {"stringValue": ""},
        "owner": {"stringValue": owner},
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
        self.documents_url = (
            f"https://firestore.googleapis.com/v1/projects/{project}/"
            f"databases/{database}/documents"
        )
        self.collection_url = f"{self.documents_url}/{COLLECTION}"
        self._cleanup_day: str | None = None

    def _document_url(self, key: str) -> str:
        return self.collection_url + "/" + key

    def create(self, key: str, now: int, *, owner: str = "") -> dict | None:
        response = self.session.request(
            "POST", self.collection_url,
            params={"documentId": key}, json=_starting_document(now, owner),
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

    def reset(self, key: str, previous: dict, now: int, *, owner: str = "") -> dict | None:
        """Compare-and-swap an explicitly retried failed or stale claim."""
        response = self.session.request(
            "PATCH", self._document_url(key),
            params={"currentDocument.updateTime": previous["updateTime"]},
            json=_starting_document(now, owner), timeout=20,
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
                "owner": fields.get("owner", {"stringValue": ""}),
            }},
            timeout=20,
        )
        response.raise_for_status()

    def owns_operation(self, owner: str, operation: str) -> bool:
        if not operation:
            return False
        response = self.session.request(
            "POST", self.documents_url + ":runQuery", timeout=20,
            json={"structuredQuery": {
                "from": [{"collectionId": COLLECTION}],
                "select": {"fields": [{"fieldPath": "owner"}]},
                "where": {"fieldFilter": {
                    "field": {"fieldPath": "operation"}, "op": "EQUAL",
                    "value": {"stringValue": operation},
                }},
                "limit": 1,
            }},
        )
        response.raise_for_status()
        return any(
            result.get("document", {}).get("fields", {}).get("owner", {}).get("stringValue") == owner
            for result in response.json()
        )

    def sweep_expired(self, now: int) -> None:
        """Delete a bounded batch on the first request of each UTC day."""
        instant = datetime.fromtimestamp(now, tz=timezone.utc)
        day = instant.date().isoformat()
        if self._cleanup_day == day:
            return
        marker = self.read(_CLEANUP_MARKER)
        if marker and marker["fields"]["day"]["stringValue"] >= day:
            self._cleanup_day = day
            return
        precondition = (
            {"currentDocument.updateTime": marker["updateTime"]} if marker
            else {"currentDocument.exists": "false"}
        )
        # Claim the day's sweep before deleting so concurrent instances share the cap.
        response = self.session.request(
            "PATCH", self._document_url(_CLEANUP_MARKER), params=precondition,
            json={"fields": {"day": {"stringValue": day}}}, timeout=20,
        )
        if response.status_code in (409, 412):
            return
        response.raise_for_status()
        self._cleanup_day = day

        response = self.session.request(
            "POST", self.documents_url + ":runQuery", timeout=20,
            json={"structuredQuery": {
                "from": [{"collectionId": COLLECTION}],
                "select": {"fields": [{"fieldPath": "expires_at"}]},
                "where": {"fieldFilter": {
                    "field": {"fieldPath": "expires_at"}, "op": "LESS_THAN_OR_EQUAL",
                    "value": {"timestampValue": instant.isoformat().replace("+00:00", "Z")},
                }},
                "limit": _CLEANUP_LIMIT,
            }},
        )
        response.raise_for_status()
        writes = [
            {"delete": document["name"], "currentDocument": {"updateTime": document["updateTime"]}}
            for result in response.json() if (document := result.get("document"))
        ]
        if writes:
            response = self.session.request(
                "POST", self.documents_url + ":batchWrite", json={"writes": writes}, timeout=20,
            )
            response.raise_for_status()
            # A competing retry can reset a claim between the query and deletion.
            if any(status.get("code", 0) not in (0, 5, 9) for status in response.json()["status"]):
                raise RuntimeError("Firestore could not delete one or more expired claims")


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

    def status(self, operation: str, *, owner: str = "") -> dict:
        if owner and not self.claims.owns_operation(owner, operation):
            raise PermissionError("This operation does not belong to the connected account")
        return self.backend.status(operation)

    def _submit(
        self, key: str, claim: dict, file_id: str,
        keywords: tuple[str, ...], prompt: str, language: str, replace_existing: bool,
        drive_envelope: str,
    ) -> dict:
        # If Cloud Run accepted this request but its response was lost, the
        # claim stays pending. This is intentionally not auto-retried.
        result = self.backend.start(
            file_id, keywords=keywords, prompt=prompt, language=language,
            replace_existing=replace_existing,
            **({"drive_envelope": drive_envelope} if drive_envelope else {}),
        )
        try:
            self.claims.publish(key, claim, result["operation"])
        except Exception:
            # Linked-mode status requires a confirmed owner record.
            if drive_envelope:
                result.pop("operation", None)
            result["warning"] = (
                "Job started but its duplicate-protection record could not be confirmed. Do not submit again."
            )
        return result

    def start(
        self, file_id: str, keywords: list[str] | tuple[str, ...] | None = None,
        prompt: str = "", retry: bool = False, language: str = "auto",
        *, replace_existing: bool = False, owner: str = "", drive_envelope: str = "",
    ) -> dict:
        if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
            raise ValueError("A valid Google Drive file ID is required")
        normalized, prompt = normalize_transcription_context(keywords, prompt)
        language = normalize_language(language)
        if not isinstance(retry, bool):
            raise ValueError("retry must be a boolean")
        try:
            self.claims.sweep_expired(int(self.clock()))
        except Exception as exc:
            logger.warning("Expired claim cleanup failed (%s)", type(exc).__name__)

        def submit(claim: dict) -> dict:
            return self._submit(
                key, claim, file_id, normalized, prompt, language, replace_existing, drive_envelope,
            )

        key = hashlib.sha256(file_id.encode("utf-8")).hexdigest()
        ownership = {"owner": owner} if owner else {}
        for _ in range(4):
            now = int(self.clock())
            claim = self.claims.create(key, now, **ownership)
            if claim is not None:
                return submit(claim)

            existing = self.claims.read(key)
            if existing is None:
                continue  # raced a removal; attempt the atomic create again

            try:
                age = now - int(existing["fields"]["started_at"]["integerValue"])
            except (KeyError, TypeError, ValueError):
                age = 0  # invalid data fails closed
            operation = _recorded_operation(existing)
            visible_operation = (
                {"operation": operation}
                if not owner or existing["fields"].get("owner", {}).get("stringValue") == owner
                else {}
            )
            if operation:
                try:
                    current = self.backend.status(operation)
                except Exception:
                    # An operation may eventually expire from the Cloud Run API.
                    # A deliberate retry after the safety window is possible,
                    # but never automatically triggered.
                    if retry and age >= UNKNOWN_SUBMISSION_RETRY_SECONDS:
                        claim = self.claims.reset(key, existing, now, **ownership)
                        if claim is not None:
                            return submit(claim)
                        continue
                    return {
                        "status": "operation_unavailable",
                        **visible_operation,
                        "duplicate": True,
                        "retry_requires_confirmation": True,
                    }
                state = current.get("status")
                if state in ("failed", "completed") and retry:
                    claim = self.claims.reset(key, existing, now, **ownership)
                    if claim is not None:
                        return submit(claim)
                    continue
                return {
                    "status": (
                        "previous_failed" if state == "failed"
                        else "already_completed" if state == "completed"
                        else "already_running"
                    ),
                    **visible_operation,
                    "duplicate": True,
                }

            # A previous request may have launched a job but lost the response
            # before publishing the operation. Never auto-release this claim.
            if retry and age >= UNKNOWN_SUBMISSION_RETRY_SECONDS:
                claim = self.claims.reset(key, existing, now, **ownership)
                if claim is not None:
                    return submit(claim)
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
