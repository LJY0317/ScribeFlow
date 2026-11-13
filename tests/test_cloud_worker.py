import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scribeflow.cloud_worker import main, run_job


class CloudWorkerTests(unittest.TestCase):
    def test_invalid_file_does_not_call_transcription(self):
        with patch.dict(os.environ, {
            "SCRIBEFLOW_SOURCE_FILE_ID": "../file",
            "SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID": "folder_" + "a" * 25,
            "OPENAI_API_KEY": "fake",
            "SCRIBEFLOW_RCLONE_CONFIG_SECRET": "/not/present",
        }):
            with patch("scribeflow.cloud_worker.transcribe") as transcribe:
                with self.assertRaises(ValueError):
                    run_job()
                transcribe.assert_not_called()

    def test_secret_is_private_and_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "config-secret"
            original.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
            with patch.dict(os.environ, {
                "SCRIBEFLOW_SOURCE_FILE_ID": "audio_" + "a" * 25,
                "SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID": "folder_" + "b" * 25,
                "OPENAI_API_KEY": "fake",
                "SCRIBEFLOW_RCLONE_CONFIG_SECRET": str(original),
            }):
                private_paths = []

                def fake_transcribe(file_id):
                    self.assertEqual(file_id, "audio_" + "a" * 25)
                    config = Path(os.environ["RCLONE_CONFIG"])
                    self.assertTrue(config.exists())
                    self.assertEqual(config.read_bytes(), original.read_bytes())
                    self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                    private_paths.append(config.parent)
                    self.assertEqual(Path(os.environ["SCRIBEFLOW_CACHE_DIR"]).parent, config.parent)
                    return {"ok": True, "status": "created"}

                with patch("scribeflow.cloud_worker.transcribe", side_effect=fake_transcribe):
                    self.assertEqual(run_job()["status"], "created")
                self.assertFalse(private_paths[0].exists())
                self.assertEqual(original.read_text(), "[gdrive]\ntype = drive\n")

    def test_job_error_does_not_log_secret(self):
        with patch("scribeflow.cloud_worker.run_job", side_effect=RuntimeError("API KEY SECRET")):
            with patch("sys.stderr") as stream:
                self.assertEqual(main(), 1)
            self.assertNotIn("API KEY SECRET", "".join(str(c) for c in stream.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
