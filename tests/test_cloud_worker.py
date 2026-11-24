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
                        "keywords": (), "prompt": "", "language": "auto", "replace_existing": False,
                        "retain_chunks": False,
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
                    replace_existing=False, retain_chunks=False,
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
                "SCRIBEFLOW_REPLACE_EXISTING": "true",
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
                        replace_existing=True, retain_chunks=False,
                    )

    def test_external_transcript_id_reaches_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
            with patch.dict(os.environ, {
                "SCRIBEFLOW_SOURCE_FILE_ID": "audio_" + "a" * 25,
                "SCRIBEFLOW_TRANSCRIPT_FILE_ID": "text_" + "b" * 25,
                "SCRIBEFLOW_RCLONE_CONFIG_SECRET": str(config),
                "SCRIBEFLOW_REPLACE_EXISTING": "true", "OPENAI_API_KEY": "fake",
            }):
                with patch("scribeflow.cloud_worker.transcribe", return_value={"ok": True}) as transcribe:
                    run_job()
                self.assertEqual(
                    transcribe.call_args.kwargs["transcript_file_id"], "text_" + "b" * 25
                )

    def test_markdown_and_replacement_do_not_require_parent_create_permission(self):
        from scribeflow.linked_drive import DriveCredential
        from cryptography.fernet import Fernet
        from types import SimpleNamespace
        key = Fernet.generate_key().decode()
        audio_id = "audio_" + "a" * 25
        with patch.dict(os.environ, {
            "SCRIBEFLOW_SOURCE_FILE_ID": audio_id,
            "SCRIBEFLOW_LINKED_DRIVE_ENVELOPE": "sealed",
            "SCRIBEFLOW_DRIVE_JOB_KEY": key,
            "SCRIBEFLOW_REPLACE_EXISTING": "true",
            "SCRIBEFLOW_OUTPUT_FORMAT": "md",
            "OPENAI_API_KEY": "fake",
        }), patch("scribeflow.cloud_worker.DriveCredential.open", return_value=SimpleNamespace(
            rclone_config=lambda _: "[gdrive]\ntype = drive\n",
        )), patch("scribeflow.cloud_worker.transcribe_drive", return_value={"ok": True}) as call:
            self.assertTrue(run_job()["ok"])
            self.assertEqual(call.call_args.kwargs["output_format"], "md")
            self.assertTrue(call.call_args.kwargs["replace_existing"])

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
            with patch("sys.stdout") as stream:
                self.assertEqual(main(), 1)
            self.assertNotIn("API KEY SECRET", "".join(str(c) for c in stream.write.call_args_list))

    def test_result_record_failure_does_not_invalidate_saved_transcript(self):
        file_id = "text_" + "a" * 25
        with patch("scribeflow.cloud_worker.run_job", return_value={"ok": True, "status": "created", "output_format": "md", "transcript_file_id": file_id}), \
             patch("scribeflow.cloud_worker._publish_result", side_effect=RuntimeError("private error")) as publish, \
             patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(main(), 0)
        self.assertEqual(publish.call_args.args[0]["transcript_file_id"], file_id)


if __name__ == "__main__":
    unittest.main()
