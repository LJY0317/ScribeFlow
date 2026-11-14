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

                def fake_transcribe(file_id, **kwargs):
                    self.assertEqual(file_id, "audio_" + "a" * 25)
                    self.assertEqual(kwargs, {
                        "keywords": (), "prompt": "", "language": "auto",
                    })
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

    def test_entire_drive_requires_no_folder_id(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config-secret"
            config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
            with patch.dict(os.environ, {
                "SCRIBEFLOW_SOURCE_FILE_ID": "audio_" + "a" * 25,
                "SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID": "",
                "OPENAI_API_KEY": "fake",
                "SCRIBEFLOW_RCLONE_CONFIG_SECRET": str(config),
            }):
                with patch("scribeflow.cloud_worker.transcribe", return_value={"ok": True}) as transcribe:
                    self.assertTrue(run_job()["ok"])
                transcribe.assert_called_once_with(
                    "audio_" + "a" * 25, keywords=(), prompt="", language="auto",
                )

    def test_context_from_job_environment_reaches_transcription(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config-secret"
            config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
            with patch.dict(os.environ, {
                "SCRIBEFLOW_SOURCE_FILE_ID": "audio_" + "a" * 25,
                "OPENAI_API_KEY": "fake",
                "SCRIBEFLOW_RCLONE_CONFIG_SECRET": str(config),
                "SCRIBEFLOW_KEYWORDS_JSON": '["Kubernetes", "OAuth"]',
                "SCRIBEFLOW_PROMPT": "Technical discussion",
                "SCRIBEFLOW_LANGUAGE": "en",
            }):
                with patch(
                    "scribeflow.cloud_worker.transcribe", return_value={"ok": True}
                ) as transcribe:
                    self.assertTrue(run_job()["ok"])
                    transcribe.assert_called_once_with(
                        "audio_" + "a" * 25,
                        keywords=("Kubernetes", "OAuth"),
                        prompt="Technical discussion",
                        language="en",
                    )

    def test_invalid_context_stops_before_transcription(self):
        with patch.dict(os.environ, {
            "SCRIBEFLOW_SOURCE_FILE_ID": "audio_" + "a" * 25,
            "SCRIBEFLOW_KEYWORDS_JSON": '{"not": "a list"}',
        }):
            with patch("scribeflow.cloud_worker.transcribe") as transcribe:
                with self.assertRaises(ValueError):
                    run_job()
                transcribe.assert_not_called()

    def test_job_error_does_not_log_secret(self):
        with patch("scribeflow.cloud_worker.run_job", side_effect=RuntimeError("API KEY SECRET")):
            with patch("sys.stderr") as stream:
                self.assertEqual(main(), 1)
            self.assertNotIn("API KEY SECRET", "".join(str(c) for c in stream.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
