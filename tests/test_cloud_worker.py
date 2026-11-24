import os
import unittest
from unittest.mock import patch

from scribeflow.cloud_worker import main, run_job


class CloudWorkerTests(unittest.TestCase):
    def setUp(self):
        self.audio_id = "audio_" + "a" * 25
        self.env = {
            "SCRIBEFLOW_SOURCE_FILE_ID": self.audio_id,
            "SCRIBEFLOW_LINKED_DRIVE_ENVELOPE": "sealed",
            "SCRIBEFLOW_REQUEST_OWNER": "google-user",
            "OPENAI_API_KEY": "fake",
        }

    def test_invalid_request_stops_before_transcription(self):
        for overrides in (
            {"SCRIBEFLOW_SOURCE_FILE_ID": "../file"},
            {"SCRIBEFLOW_KEYWORDS_JSON": '{"not": "a list"}'},
            {"SCRIBEFLOW_LINKED_DRIVE_ENVELOPE": ""},
            {"SCRIBEFLOW_REQUEST_OWNER": ""},
        ):
            with self.subTest(overrides=overrides), patch.dict(
                os.environ, {**self.env, **overrides}, clear=True
            ), patch("scribeflow.cloud_worker.transcribe_drive") as transcribe:
                with self.assertRaises(ValueError):
                    run_job()
                transcribe.assert_not_called()

    def test_context_and_target_reach_direct_drive_transcription(self):
        credential = object()
        with patch.dict(os.environ, {
            **self.env,
            "SCRIBEFLOW_KEYWORDS_JSON": '["Kubernetes", "OAuth"]',
            "SCRIBEFLOW_PROMPT": "Technical discussion",
            "SCRIBEFLOW_LANGUAGE": "en",
            "SCRIBEFLOW_REPLACE_EXISTING": "true",
            "SCRIBEFLOW_OUTPUT_FORMAT": "md",
            "SCRIBEFLOW_TRANSCRIPT_FILE_ID": "text_" + "b" * 25,
        }, clear=True), patch(
            "scribeflow.cloud_worker.DriveCredential.open", return_value=credential
        ) as open_credential, patch(
            "scribeflow.cloud_worker.transcribe_drive", return_value={"ok": True}
        ) as transcribe:
            self.assertTrue(run_job()["ok"])
            open_credential.assert_called_once_with("sealed", self.audio_id)
            private = transcribe.call_args.args[2]
            transcribe.assert_called_once_with(
                credential, self.audio_id, private,
                keywords=("Kubernetes", "OAuth"), prompt="Technical discussion",
                language="en", replace_existing=True, output_format="md",
                transcript_file_id="text_" + "b" * 25,
            )
            self.assertFalse(private.exists())

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
