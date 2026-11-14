"""Release preflight uses mock binaries; never touches a real GCP project."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PREFLIGHT = Path(__file__).resolve().parents[1] / "scripts" / "preflight-cloud-run.sh"
HEAD = "852053bfc6c849846eae416fa3a667ace224cc06"


class CloudPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.calls = root / "cloud-calls.log"
        self.stub(
            "git",
            r"""#!/bin/sh
case "$1" in
    branch) printf '%s\n' "${FAKE_BRANCH:-main}" ;;
    status) printf '%s' "${FAKE_DIRTY:-}" ;;
    rev-parse) printf '%s\n' "${FAKE_LOCAL_SHA}" ;;
    ls-remote) printf '%s\trefs/heads/main\n' "${FAKE_REMOTE_SHA}" ;;
    *) echo "Unexpected git command: $*" >&2; exit 9 ;;
esac
""",
        )
        self.stub(
            "gcloud",
            r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_CLOUD_LOG"
case "$1 $2 $3" in
    "billing projects describe") printf 'True\n' ;;
    "run services describe") printf '{"spec":{"template":{"spec":{"containers":[{"image":"asia-northeast3-docker.pkg.dev/demo/scribeflow/scribeflow:old"}]}}}}\n' ;;
    "run jobs describe") printf '{"spec":{"template":{"containers":[{"image":"asia-northeast3-docker.pkg.dev/demo/scribeflow/scribeflow:old"}]}}}\n' ;;
    "run jobs executions") printf 'No recent executions\n' ;;
    *) : ;;
esac
""",
        )

    def stub(self, name, content):
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run_preflight(self, **env_overrides):
        env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "FAKE_CLOUD_LOG": str(self.calls),
            "FAKE_LOCAL_SHA": HEAD,
            "FAKE_REMOTE_SHA": HEAD,
            "GCP_PROJECT": "example-project",
            "SCRIBEFLOW_GOOGLE_CLIENT_ID": "test-id.apps.googleusercontent.com",
            "SCRIBEFLOW_GOOGLE_EMAIL": "test@example.org",
        }
        env.update(env_overrides)
        return subprocess.run(
            ["bash", str(PREFLIGHT)],
            env=env, capture_output=True, text=True, timeout=20,
        )

    def test_success_is_read_only_and_prints_only_images_not_secrets(self):
        result = self.run_preflight()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("READ-ONLY PREFLIGHT PASSED", result.stdout)
        self.assertIn("Existing image(s):", result.stdout)
        self.assertNotIn("test-id.apps", result.stdout)
        self.assertNotIn("test@example.org", result.stdout)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(calls), 10)
        read_only_prefixes = (
            "projects describe", "billing projects describe",
            "run services describe", "run jobs describe",
            "run jobs executions list", "firestore databases describe",
            "artifacts repositories describe", "secrets versions describe",
        )
        self.assertTrue(all(
            command.startswith(read_only_prefixes) for command in calls
        ), calls)

    def test_stale_or_dirty_or_nonmain_checkouts_never_call_cloud(self):
        cases = [
            {"FAKE_BRANCH": "feature"},
            {"FAKE_DIRTY": " M README.md"},
            {"FAKE_REMOTE_SHA": "f" * 40},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.calls.unlink(missing_ok=True)
                result = self.run_preflight(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_unset_oauth_settings_stop_before_cloud_deployment(self):
        result = self.run_preflight(SCRIBEFLOW_GOOGLE_EMAIL="")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("READ-ONLY PREFLIGHT PASSED", result.stdout)

    def test_missing_image_reference_is_not_mistaken_for_valid_deployment(self):
        self.stub(
            "gcloud",
            r"""#!/bin/sh
if [ "$1 $2 $3" = "billing projects describe" ]; then
  echo True
elif [ "$1 $2 $3" = "run services describe" ]; then
  echo '{}'
else
  echo '{}'
fi
""",
        )
        result = self.run_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not identify current deployed image", result.stderr)


if __name__ == "__main__":
    unittest.main()
