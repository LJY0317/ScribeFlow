"""Idempotency and race tests. No real Firestore or paid Cloud Run calls."""

import copy
import hashlib
import threading
import unittest
from functools import partial
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
        self.results = {}
        self.revision = 0
        self.sweep_expired = Mock()

    def _snapshot(self, value):
        return copy.deepcopy(value)

    def _new(self, now, owner, output_format):
        self.revision += 1
        result = _starting_document(now, owner, output_format)
        result["updateTime"] = str(self.revision)
        return result

    def create(self, key, now, *, owner="", output_format="txt"):
        with self.lock:
            if key in self.documents:
                return None
            self.documents[key] = self._new(now, owner, output_format)
            return self._snapshot(self.documents[key])

    def read(self, key):
        with self.lock:
            value = self.documents.get(key)
            return self._snapshot(value) if value else None

    def reset(self, key, previous, now, *, owner="", output_format="txt"):
        with self.lock:
            current = self.documents.get(key)
            if not current or current["updateTime"] != previous["updateTime"]:
                return None
            self.documents[key] = self._new(now, owner, output_format)
            return self._snapshot(self.documents[key])

    def owns_operation(self, owner, operation):
        with self.lock:
            return any(
                record["fields"]["operation"]["stringValue"] == operation
                and record["fields"]["owner"]["stringValue"] == owner
                for record in self.documents.values()
            )

    def publish(self, key, claim, operation):
        with self.lock:
            current = self.documents[key]
            if current["updateTime"] != claim["updateTime"]:
                raise RuntimeError("competing claim")
            current["fields"]["operation"] = {"stringValue": operation}
            self.revision += 1
            current["updateTime"] = str(self.revision)

    def read_result(self, execution):
        return self.results.get(execution)


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
        self.start = partial(self.jobs.start, owner="google-a", drive_envelope="encrypted-a")

    def test_expired_claim_cleanup_failure_does_not_stop_submission(self):
        self.claims.sweep_expired.side_effect = RuntimeError("maintenance unavailable")
        with self.assertLogs("scribeflow.cloud_dedup", level="WARNING"):
            self.assertEqual(self.start(FILE_ID)["status"], "submitted")
        self.backend.start.assert_called_once()

    def test_second_submission_returns_same_running_operation(self):
        self.assertEqual(self.start(FILE_ID)["status"], "submitted")
        repeated = self.start(FILE_ID, keywords=["보험"])
        self.assertEqual(repeated, {
            "status": "already_running", "operation": OPERATION,
            "duplicate": True,
        })
        self.backend.start.assert_called_once_with(
            FILE_ID, keywords=(), prompt="", language="auto", replace_existing=False,
            owner="google-a", drive_envelope="encrypted-a",
        )
        self.assertEqual(len(self.claims.documents), 1)
        self.assertEqual(
            next(iter(self.claims.documents)),
            hashlib.sha256(FILE_ID.encode()).hexdigest(),
        )

    def test_shared_source_deduplicates_without_revealing_the_other_operation(self):
        self.start(FILE_ID, owner="google-a", drive_envelope="encrypted-a")
        duplicate = self.start(FILE_ID, owner="google-b", drive_envelope="encrypted-b")
        self.assertEqual(duplicate, {"status": "already_running", "duplicate": True})
        self.backend.start.assert_called_once()
        self.assertEqual(self.jobs.status(OPERATION, owner="google-a")["status"], "running")
        with self.assertRaises(PermissionError):
            self.jobs.status(OPERATION, owner="google-b")

    def test_completed_source_rechecks_current_drive_and_model(self):
        self.start(FILE_ID)
        self.backend.status.return_value = {
            "status": "completed", "operation": OPERATION,
        }
        self.assertEqual(self.start(FILE_ID)["status"], "submitted")
        self.assertEqual(self.backend.start.call_count, 2)

    def test_actual_result_format_and_owner_survive_retranscription(self):
        self.start(FILE_ID, owner="google-a", drive_envelope="encrypted-a")
        self.backend.status.return_value = {"status": "completed", "operation": OPERATION, "execution": "execution-a"}
        receipt = {"result": "created", "output_format": "txt", "transcript_file_id": "output-id"}
        self.claims.results["execution-a"] = ("google-a", receipt)
        previous = self.jobs.status(OPERATION, owner="google-a")
        self.assertEqual(previous["output_format"], "txt")
        self.assertEqual(previous["transcript_file_id"], "output-id")
        self.backend.start.assert_called_once()
        self.backend.start.return_value = {"status": "submitted", "operation": OPERATION + "-new"}
        self.start(FILE_ID, retry=True, replace_existing=True, owner="google-a", drive_envelope="encrypted-a")
        self.assertEqual(self.jobs.status(OPERATION, owner="google-a")["transcript_file_id"], "output-id")
        with self.assertRaises(PermissionError):
            self.jobs.status(OPERATION, owner="google-b")

    def test_missing_result_does_not_change_completed_execution(self):
        self.start(FILE_ID, owner="google-a", drive_envelope="encrypted-a")
        self.backend.status.return_value = {"status": "completed", "operation": OPERATION, "execution": "execution-a"}
        self.assertEqual(self.jobs.status(OPERATION, owner="google-a"), {
            "status": "completed", "operation": OPERATION, "result_available": False,
        })

    def test_completed_job_rechecks_without_explicit_retry(self):
        self.start(FILE_ID)
        self.backend.status.return_value = {
            "status": "completed", "operation": OPERATION,
        }
        self.assertEqual(
            self.start(FILE_ID)["status"], "submitted",
        )
        self.assertEqual(self.backend.start.call_count, 2)
        self.assertEqual(
            self.start(FILE_ID, retry=True)["status"], "submitted",
        )
        self.assertEqual(self.backend.start.call_count, 3)

    def test_explicit_retry_after_failure_replaces_claim(self):
        self.start(FILE_ID)
        self.backend.status.return_value = {
            "status": "failed", "operation": OPERATION,
        }
        self.assertEqual(self.start(FILE_ID)["status"], "previous_failed")
        self.backend.start.assert_called_once()
        self.assertEqual(
            self.start(FILE_ID, retry=True)["status"], "submitted",
        )
        self.assertEqual(self.backend.start.call_count, 2)

    def test_uncertain_network_response_does_not_auto_resubmit(self):
        self.backend.start.side_effect = TimeoutError("unknown Cloud Run result")
        with self.assertRaises(TimeoutError):
            self.start(FILE_ID)
        self.backend.start.reset_mock(side_effect=True)
        self.assertEqual(self.start(FILE_ID)["status"], "already_submitting")
        self.now += UNKNOWN_SUBMISSION_RETRY_SECONDS + 1
        self.assertEqual(
            self.start(FILE_ID)["status"], "submission_unconfirmed",
        )
        self.backend.start.assert_not_called()
        self.backend.start.return_value = {
            "status": "submitted", "operation": OPERATION,
        }
        self.assertEqual(
            self.start(FILE_ID, retry=True)["status"], "submitted",
        )
        self.backend.start.assert_called_once()

    def test_status_outage_never_launches_a_duplicate(self):
        self.start(FILE_ID)
        self.backend.status.side_effect = RuntimeError("cloud API offline")
        repeated = self.start(FILE_ID)
        self.assertEqual(repeated["status"], "operation_unavailable")
        self.assertTrue(repeated["retry_requires_confirmation"])
        self.backend.start.assert_called_once()

    def test_expired_operation_must_be_explicitly_retried(self):
        self.start(FILE_ID)
        self.backend.status.side_effect = RuntimeError("operation no longer available")
        self.now += UNKNOWN_SUBMISSION_RETRY_SECONDS + 10
        old = self.start(FILE_ID)
        self.assertEqual(old["status"], "operation_unavailable")
        self.backend.start.assert_called_once()
        self.backend.status.reset_mock(side_effect=True)
        self.backend.status.side_effect = RuntimeError("expired")
        self.assertEqual(
            self.start(FILE_ID, retry=True)["status"], "submitted",
        )
        self.assertEqual(self.backend.start.call_count, 2)

    def test_claim_expiration_is_seven_days_and_extended_on_retry(self):
        from datetime import datetime, timezone

        first = self.start(FILE_ID)
        self.assertEqual(first["status"], "submitted")
        key = next(iter(self.claims.documents))
        record = self.claims.read(key)
        expiry = datetime.fromisoformat(
            record["fields"]["expires_at"]["timestampValue"].replace("Z", "+00:00")
        )
        self.assertEqual(
            (expiry - datetime.fromtimestamp(self.now, tz=timezone.utc)).days, 7,
        )
        self.backend.status.return_value = {
            "status": "failed", "operation": OPERATION,
        }
        self.now += 100
        self.start(FILE_ID, retry=True)
        updated = self.claims.read(key)
        self.assertNotEqual(
            record["fields"]["expires_at"],
            updated["fields"]["expires_at"],
        )

    def test_claim_store_outage_fails_closed_before_billable_request(self):
        self.claims.create = Mock(side_effect=RuntimeError("Firestore unavailable"))
        with self.assertRaises(RuntimeError):
            self.start(FILE_ID)
        self.backend.start.assert_not_called()

    def test_reject_invalid_options_before_reserving(self):
        for kwargs in (
            {"file_id": "../wrong-id"},
            {"file_id": FILE_ID, "keywords": [""]},
            {"file_id": FILE_ID, "retry": "yes"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.start(**kwargs)
        self.assertEqual(self.claims.documents, {})
        self.backend.start.assert_not_called()

    def test_failed_claim_publication_keeps_job_operation_visible(self):
        self.claims.publish = Mock(side_effect=RuntimeError("Firestore offline"))
        result = self.start(FILE_ID)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["operation"], OPERATION)
        self.assertIn("warning", result)
        self.assertEqual(self.start(FILE_ID)["status"], "already_submitting")
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
            first = pool.submit(self.start, FILE_ID)
            self.assertTrue(started.wait(timeout=5))
            second = pool.submit(self.start, FILE_ID)
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
        self.assertIn("expires_at", kwargs["json"]["fields"])
        self.assertIn("timestampValue", kwargs["json"]["fields"]["expires_at"])

    def test_result_receipt_uses_existing_collection_and_expiration(self):
        self.session.request.return_value = self.response(200)
        receipt = {"result": "created", "transcript_file_id": "output-id", "output_format": "md"}
        self.claims.write_result("execution-a", "owner-a", receipt)
        args, kwargs = self.session.request.call_args
        self.assertTrue(args[1].endswith("/result_" + hashlib.sha256(b"execution-a").hexdigest()))
        fields = kwargs["json"]["fields"]
        self.assertIn("expires_at", fields)
        self.session.request.return_value = self.response(200, {"fields": fields})
        self.assertEqual(self.claims.read_result("execution-a"), ("owner-a", receipt))

    def test_sweep_filters_expired_records_and_deletes_only_queried_versions(self):
        document = {"name": "projects/p/databases/db/documents/scribeflow_job_claims/old",
                    "updateTime": "2026-11-13T00:00:00Z"}
        self.session.request.side_effect = [
            self.response(404), self.response(200),
            self.response(200, [{"document": document}, {"readTime": "ignored"}]),
            self.response(200, {"status": [{"code": 9}]}),
        ]
        self.claims.sweep_expired(0)
        self.claims.sweep_expired(1)
        _, marker, query, delete = self.session.request.call_args_list
        self.assertEqual(marker.kwargs["params"], {"currentDocument.exists": "false"})
        request = query.kwargs["json"]["structuredQuery"]
        self.assertEqual(request["limit"], 100)
        self.assertEqual(request["from"], [{"collectionId": "scribeflow_job_claims"}])
        self.assertEqual(request["where"]["fieldFilter"], {
            "field": {"fieldPath": "expires_at"}, "op": "LESS_THAN_OR_EQUAL",
            "value": {"timestampValue": "1970-01-01T00:00:00Z"},
        })
        self.assertEqual(delete.kwargs["json"], {"writes": [{
            "delete": document["name"], "currentDocument": {"updateTime": document["updateTime"]},
        }]})

    def test_sweep_does_not_run_when_another_instance_reserved_the_day(self):
        self.session.request.return_value = self.response(200, {
            "fields": {"day": {"stringValue": "1970-01-01"}},
        })
        self.claims.sweep_expired(0)
        self.session.request.assert_called_once()

    def test_sweep_lost_race_does_not_delete_anything(self):
        self.session.request.side_effect = [self.response(404), self.response(412)]
        self.claims.sweep_expired(0)
        self.assertEqual(self.session.request.call_count, 2)



if __name__ == "__main__":
    unittest.main()
