"""Idempotency and race tests. No real Firestore or paid Cloud Run calls."""

import copy
import hashlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from scribeflow.cloud_dedup import (
    UNKNOWN_SUBMISSION_RETRY_SECONDS,
    DeduplicatingCloudRunJobs,
    FirestoreJobClaims,
    _starting_document,
)


class FakeClaims:
    """Atomic, versioned in-memory equivalent of the Firestore claim adapter."""

    def __init__(self):
        self.lock = threading.Lock()
        self.documents = {}
        self.revision = 0

    def _snapshot(self, value):
        return copy.deepcopy(value)

    def _new(self, now):
        self.revision += 1
        result = _starting_document(now)
        result["updateTime"] = str(self.revision)
        return result

    def create(self, key, now):
        with self.lock:
            if key in self.documents:
                return None
            self.documents[key] = self._new(now)
            return self._snapshot(self.documents[key])

    def read(self, key):
        with self.lock:
            value = self.documents.get(key)
            return self._snapshot(value) if value else None

    def reset(self, key, previous, now):
        with self.lock:
            current = self.documents.get(key)
            if not current or current["updateTime"] != previous["updateTime"]:
                return None
            self.documents[key] = self._new(now)
            return self._snapshot(self.documents[key])

    def publish(self, key, claim, operation):
        with self.lock:
            current = self.documents[key]
            if current["updateTime"] != claim["updateTime"]:
                raise RuntimeError("competing claim")
            current["fields"]["operation"] = {"stringValue": operation}
            self.revision += 1
            current["updateTime"] = str(self.revision)


FILE_ID = "file_" + "a" * 26
OPERATION = "projects/p/locations/r/operations/id1234"


class DeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.backend.start.return_value = {
            "status": "submitted", "operation": OPERATION,
        }
        self.backend.status.return_value = {
            "status": "running", "operation": OPERATION,
        }
        self.claims = FakeClaims()
        self.now = 1_000_000
        self.jobs = DeduplicatingCloudRunJobs(
            self.backend, self.claims, clock=lambda: self.now,
        )

    def test_second_submission_returns_same_running_operation(self):
        self.assertEqual(self.jobs.start(FILE_ID)["status"], "submitted")
        repeated = self.jobs.start(FILE_ID, keywords=["보험"])
        self.assertEqual(repeated, {
            "status": "already_running", "operation": OPERATION,
            "duplicate": True,
        })
        self.backend.start.assert_called_once_with(
            FILE_ID, keywords=(), prompt="",
        )
        self.assertEqual(len(self.claims.documents), 1)
        self.assertEqual(
            next(iter(self.claims.documents)),
            hashlib.sha256(FILE_ID.encode()).hexdigest(),
        )

    def test_completed_source_does_not_relaunch_paid_job(self):
        self.jobs.start(FILE_ID)
        self.backend.status.return_value = {
            "status": "completed", "operation": OPERATION,
        }
        self.assertEqual(self.jobs.start(FILE_ID)["status"], "already_completed")
        self.backend.start.assert_called_once()

    def test_explicit_retry_after_failure_replaces_claim(self):
        self.jobs.start(FILE_ID)
        self.backend.status.return_value = {
            "status": "failed", "operation": OPERATION,
        }
        self.assertEqual(self.jobs.start(FILE_ID)["status"], "previous_failed")
        self.backend.start.assert_called_once()
        self.assertEqual(
            self.jobs.start(FILE_ID, retry=True)["status"], "submitted",
        )
        self.assertEqual(self.backend.start.call_count, 2)

    def test_uncertain_network_response_does_not_auto_resubmit(self):
        self.backend.start.side_effect = TimeoutError("unknown Cloud Run result")
        with self.assertRaises(TimeoutError):
            self.jobs.start(FILE_ID)
        self.backend.start.reset_mock(side_effect=True)
        self.assertEqual(self.jobs.start(FILE_ID)["status"], "already_submitting")
        self.now += UNKNOWN_SUBMISSION_RETRY_SECONDS + 1
        self.assertEqual(
            self.jobs.start(FILE_ID)["status"], "submission_unconfirmed",
        )
        self.backend.start.assert_not_called()
        self.backend.start.return_value = {
            "status": "submitted", "operation": OPERATION,
        }
        self.assertEqual(
            self.jobs.start(FILE_ID, retry=True)["status"], "submitted",
        )
        self.backend.start.assert_called_once()

    def test_status_outage_never_launches_a_duplicate(self):
        self.jobs.start(FILE_ID)
        self.backend.status.side_effect = RuntimeError("cloud API offline")
        repeated = self.jobs.start(FILE_ID)
        self.assertEqual(repeated["status"], "already_submitted")
        self.backend.start.assert_called_once()

    def test_claim_store_outage_fails_closed_before_billable_request(self):
        self.claims.create = Mock(side_effect=RuntimeError("Firestore unavailable"))
        with self.assertRaises(RuntimeError):
            self.jobs.start(FILE_ID)
        self.backend.start.assert_not_called()

    def test_reject_invalid_options_before_reserving(self):
        for kwargs in (
            {"file_id": "../wrong-id"},
            {"file_id": FILE_ID, "keywords": [""]},
            {"file_id": FILE_ID, "retry": "yes"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.jobs.start(**kwargs)
        self.assertEqual(self.claims.documents, {})
        self.backend.start.assert_not_called()

    def test_failed_claim_publication_keeps_job_operation_visible(self):
        self.claims.publish = Mock(side_effect=RuntimeError("Firestore offline"))
        result = self.jobs.start(FILE_ID)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["operation"], OPERATION)
        self.assertIn("warning", result)
        self.assertEqual(self.jobs.start(FILE_ID)["status"], "already_submitting")
        self.backend.start.assert_called_once()

    def test_concurrent_requests_cannot_start_twice(self):
        started = threading.Event()
        continue_job = threading.Event()

        def delayed_start(*args, **kwargs):
            started.set()
            self.assertTrue(continue_job.wait(timeout=5))
            return {"status": "submitted", "operation": OPERATION}

        self.backend.start.side_effect = delayed_start
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.jobs.start, FILE_ID)
            self.assertTrue(started.wait(timeout=5))
            second = pool.submit(self.jobs.start, FILE_ID)
            self.assertEqual(second.result(timeout=5)["status"], "already_submitting")
            continue_job.set()
            self.assertEqual(first.result(timeout=5)["status"], "submitted")
        self.backend.start.assert_called_once()


class FirestoreRestClaimsTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.claims = FirestoreJobClaims("sample-cloud-project", self.session)
        self.key = hashlib.sha256(FILE_ID.encode()).hexdigest()

    def response(self, status, document=None):
        reply = Mock()
        reply.status_code = status
        reply.json.return_value = document or {}
        if status >= 400:
            reply.raise_for_status.side_effect = RuntimeError(str(status))
        return reply

    def test_atomic_create_only_writes_hash_not_raw_file_id(self):
        created = {"updateTime": "2026-11-14T00:00:00Z", **_starting_document(10)}
        self.session.request.return_value = self.response(200, created)
        self.assertEqual(self.claims.create(self.key, 10), created)
        args, kwargs = self.session.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(kwargs["params"]["documentId"], self.key)
        self.assertNotIn(FILE_ID, repr(kwargs))
        self.assertEqual(kwargs["timeout"], 20)

    def test_conflict_does_not_overwrite_existing_claim(self):
        self.session.request.return_value = self.response(409)
        self.assertIsNone(self.claims.create(self.key, 11))

    def test_conditional_retry_uses_previous_update_time(self):
        old = {"updateTime": "2026-11-14T00:00:00Z", **_starting_document(10)}
        self.session.request.return_value = self.response(412)
        self.assertIsNone(self.claims.reset(self.key, old, 11))
        args, kwargs = self.session.request.call_args
        self.assertEqual(args[0], "PATCH")
        self.assertEqual(
            kwargs["params"]["currentDocument.updateTime"], old["updateTime"],
        )

    def test_publish_is_conditional_on_owner_version(self):
        old = {"updateTime": "2026-11-14T00:00:00Z", **_starting_document(10)}
        self.session.request.return_value = self.response(200)
        self.claims.publish(self.key, old, OPERATION)
        _, kwargs = self.session.request.call_args
        self.assertEqual(
            kwargs["params"]["currentDocument.updateTime"], old["updateTime"],
        )
        self.assertEqual(
            kwargs["json"]["fields"]["operation"]["stringValue"], OPERATION,
        )


if __name__ == "__main__":
    unittest.main()
