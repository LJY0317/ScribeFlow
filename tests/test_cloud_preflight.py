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
    "run jobs list") printf '%s' "${FAKE_EXISTING_JOB:-}" ;;
    "run jobs describe") echo old-worker ;;
    "run jobs update"|"run services update"|"services enable "*) : ;;
    *) echo "Unexpected command: $*" >&2; exit 9 ;;
esac
"""
        )
        stub.chmod(0o755)

    def run_script(self, script="update-cloud-run.sh", *args, **overrides):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SCRIBEFLOW_") and k != "OPENAI_BASE_URL"}
        env.update({
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "FAKE_CLOUD_LOG": str(self.calls),
            "FAKE_SERVICES": str(self.fixture),
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


if __name__ == "__main__":
    unittest.main()
