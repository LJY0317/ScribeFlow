"""Deployment helpers use mock commands; no real Cloud resources are changed."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
IMAGE = "ghcr.io/example/scribeflow:sha-" + "a" * 40


class CloudDeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.calls = self.root / "calls"
        self.service = {
            "metadata": {"name": "scribeflow-mcp", "labels": {"cloud.googleapis.com/location": "asia-northeast3"}},
            "spec": {"template": {"spec": {"containers": [{
                "image": "old-gateway",
                "env": [
                    {"name": "SCRIBEFLOW_REGION", "value": "us-central1"},
                    {"name": "SCRIBEFLOW_JOB", "value": "scribeflow-transcribe"},
                    {"name": "SCRIBEFLOW_GOOGLE_ADDITIONAL_EMAILS", "value": "private@example.org"},
                    {"name": "SCRIBEFLOW_GOOGLE_CLIENT_SECRET", "valueFrom": {"secretKeyRef": {"name": "private-secret"}}},
                ],
            }]}}},
        }
        self.fixture = self.root / "services.json"
        self.fixture.write_text(json.dumps([self.service]))
        self.workers = self.root / "jobs.json"
        self.workers.write_text(json.dumps([
            self.worker("scribeflow-transcribe", "us-central1"),
            self.worker("scribeflow-transcribe", "asia-northeast1"),
            self.worker("another-job", "asia-northeast3"),
        ]))
        self.repositories = self.root / "repositories.json"
        self.repositories.write_text(json.dumps([
            {"name": "projects/example-project/locations/us-central1/repositories/scribeflow"},
            {"name": "projects/example-project/locations/asia-northeast3/repositories/another-repo"},
        ]))
        stub = self.root / "gcloud"
        stub.write_text(
            """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_CLOUD_LOG"
case "$1 $2 $3" in
    "run services list")
        case "$*" in
            *--format=json*) cat "$FAKE_SERVICES" ;;
            *) printf '%s' "${FAKE_EXISTING_SERVICE:-}" ;;
        esac ;;
    "run jobs list")
        case "$*" in
            *--format=json*)
                if [ "${FAKE_INVENTORY_FAILURE:-}" = 1 ]; then exit 8; fi
                cat "$FAKE_JOBS" ;;
            *) printf '%s' "${FAKE_EXISTING_JOB:-}" ;;
        esac ;;
    "services list --enabled")
        if [ "${FAKE_REGISTRY_ENABLED:-}" = 1 ]; then
            echo '[{"config":{"name":"artifactregistry.googleapis.com"}}]'
        else
            echo '[]'
        fi ;;
    "artifacts repositories list") cat "$FAKE_REPOSITORIES" ;;
    "artifacts docker images"|"artifacts repositories list-cleanup-policies") echo '[]' ;;
    "logging buckets describe") echo 'retentionDays: 30' ;;
    "run services delete"|"run jobs delete")
        if [ "${FAKE_DELETE_FAILURE:-}" = 1 ] && [ "$2" = jobs ]; then exit 7; fi ;;
    "artifacts repositories delete"|"projects remove-iam-policy-binding "*|"secrets remove-iam-policy-binding "*|    "iam service-accounts describe"|"iam service-accounts delete"|    "secrets describe "*|"secrets delete "*|"firestore databases describe"|"firestore databases delete") : ;;
    "run jobs describe") echo old-worker ;;
    "run jobs update"|"run services update"|"services enable "*) : ;;
    *) echo "Unexpected command: $*" >&2; exit 9 ;;
esac
"""
        )
        stub.chmod(0o755)

    @staticmethod
    def worker(name, location):
        return {
            "metadata": {"name": name, "labels": {"cloud.googleapis.com/location": location}},
            "spec": {"template": {"spec": {"template": {"spec": {
                "serviceAccountName": "scribeflow-worker@example-project.iam.gserviceaccount.com",
                "containers": [{
                    "image": "old-worker",
                    "env": [{"name": "OPENAI_API_KEY", "valueFrom": {
                        "secretKeyRef": {"name": "existing-user-key"}
                    }}],
                }],
            }}}}},
        }

    def run_script(self, script="update-cloud-run.sh", *args, **overrides):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SCRIBEFLOW_") and k != "OPENAI_BASE_URL"}
        env.update({
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "FAKE_CLOUD_LOG": str(self.calls),
            "FAKE_SERVICES": str(self.fixture),
            "FAKE_JOBS": str(self.workers),
            "FAKE_REPOSITORIES": str(self.repositories),
            "GCP_PROJECT": "example-project",
            "SCRIBEFLOW_IMAGE": IMAGE,
        })
        env.update(overrides)
        self.calls.unlink(missing_ok=True)
        result = subprocess.run(["bash", str(SCRIPTS / script), *args],
                                env=env, capture_output=True, text=True, timeout=20)
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        return result, calls

    def test_default_updates_only_images_at_existing_regions(self):
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[1:], [
            f"run jobs update scribeflow-transcribe --image {IMAGE} --project=example-project --region=us-central1",
            f"run services update scribeflow-mcp --image {IMAGE} --project=example-project --region=asia-northeast3",
        ])
        self.assertNotIn("private@example.org", result.stdout)
        self.assertNotIn("private-secret", result.stdout)

    def test_dry_run_is_read_only_without_oauth_configuration(self):
        result, calls = self.run_script("preflight-cloud-run.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("old-gateway", result.stdout)
        self.assertIn("old-worker", result.stdout)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith("run services list "))
        self.assertTrue(calls[1].startswith("run jobs describe "))
        self.assertIn("--region=us-central1", calls[1])

    def test_worker_only_uses_same_deployed_routing(self):
        result, calls = self.run_script("deploy-worker-only.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith("run jobs update "))
        self.assertIn("--region=us-central1", calls[1])

    def test_ambiguous_or_missing_gateway_does_not_update_anything(self):
        for services in ([], [self.service, self.service]):
            with self.subTest(count=len(services)):
                self.fixture.write_text(json.dumps(services))
                result, calls = self.run_script()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(calls), 1)

    def test_explicit_gateway_selection_and_conflicting_worker_region(self):
        result, calls = self.run_script(SCRIBEFLOW_REGION="asia-northeast3",
                                        SCRIBEFLOW_WORKER_REGION="asia-northeast3")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("--region=asia-northeast3", calls[0])
        self.assertIn("differs from the existing Gateway target", result.stderr)

    def test_initial_install_does_not_overwrite_existing_resources(self):
        for existing in ({"FAKE_EXISTING_SERVICE": "scribeflow-mcp"},
                         {"FAKE_EXISTING_JOB": "scribeflow-transcribe"}):
            with self.subTest(existing=existing):
                result, calls = self.run_script("deploy-cloud-run.sh", **existing)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("update-cloud-run.sh", result.stderr)
                self.assertEqual(len(calls), 3)
                self.assertTrue(calls[0].startswith("services enable run.googleapis.com "))
                self.assertTrue(calls[1].startswith("run services list "))
                self.assertTrue(calls[2].startswith("run jobs list "))

    def test_audit_uses_deployed_regions_and_does_not_require_registry_api(self):
        result, calls = self.run_script("audit-cloud-storage.sh",
                                        SCRIBEFLOW_WORKER_REGION="asia-northeast3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Worker target: scribeflow-transcribe (us-central1)", result.stdout)
        self.assertIn("Worker scribeflow-transcribe (asia-northeast1)", result.stdout)
        self.assertIn("No legacy Artifact Registry", result.stdout)
        self.assertNotIn("another-job", result.stdout)
        self.assertNotIn("existing-user-key", result.stdout)
        self.assertEqual(len(calls), 4)
        self.assertFalse(any(call.startswith("artifacts ") for call in calls))

    def test_uninstall_preview_finds_leftover_workers_without_gateway(self):
        self.fixture.write_text("[]")
        result, calls = self.run_script("uninstall-cloud-run.sh",
                                        FAKE_REGISTRY_ENABLED="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("worker scribeflow-transcribe (us-central1)", result.stdout)
        self.assertIn("worker scribeflow-transcribe (asia-northeast1)", result.stdout)
        self.assertIn("registry scribeflow (us-central1)", result.stdout)
        self.assertNotIn("another-job", result.stdout)
        self.assertNotIn("another-repo", result.stdout)
        self.assertIn("Dry-run only", result.stdout)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(" list " in call for call in calls))

    def test_uninstall_apply_removes_owned_resources_in_actual_regions(self):
        result, calls = self.run_script("uninstall-cloud-run.sh", "--apply",
                                        FAKE_REGISTRY_ENABLED="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        deletions = [c for c in calls if c.startswith(("run jobs delete ", "run services delete "))]
        self.assertEqual(deletions, [
            "run services delete scribeflow-mcp --project=example-project --region=asia-northeast3 --quiet",
            "run jobs delete scribeflow-transcribe --project=example-project --region=asia-northeast1 --quiet",
            "run jobs delete scribeflow-transcribe --project=example-project --region=us-central1 --quiet",
        ])
        self.assertIn("artifacts repositories delete scribeflow --project=example-project --location=us-central1 --quiet", calls)
        self.assertTrue(any(c.startswith("secrets remove-iam-policy-binding existing-user-key ") for c in calls))
        self.assertFalse(any("another-job" in c or "another-repo" in c for c in calls))
        self.assertFalse(any(c.startswith("secrets delete existing-user-key ") for c in calls))
        self.assertFalse(any("secrets versions access" in c for c in calls))

    def test_uninstall_discovery_or_worker_deletion_failure_stops_shared_cleanup(self):
        for failure in ("FAKE_INVENTORY_FAILURE", "FAKE_DELETE_FAILURE"):
            with self.subTest(failure=failure):
                result, calls = self.run_script("uninstall-cloud-run.sh", "--apply",
                                                **{failure: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c.startswith(("secrets ", "iam ", "firestore ", "projects remove-")) for c in calls))
                if failure == "FAKE_INVENTORY_FAILURE":
                    self.assertTrue(all(" list " in c for c in calls))


if __name__ == "__main__":
    unittest.main()
