import unittest
from functools import partial
from unittest.mock import Mock

from scribeflow.cloud_run import CloudRunJobs, CloudRunTarget


class CloudRunTests(unittest.TestCase):
    def setUp(self):
        self.target = CloudRunTarget("sample-cloud-project", "asia-northeast3", "scribeflow-transcribe")
        self.session = Mock()
        self.api = CloudRunJobs(self.target, session=self.session)
        self.prefix = self.target.operation_prefix
        self.start = partial(self.api.start, owner="google-user", drive_envelope="sealed")

    def _response(self, payload):
        response = Mock()
        response.json.return_value = payload
        return response

    def test_validate_file_before_cloud_api_request(self):
        with self.assertRaises(ValueError):
            self.start("../not-a-file-id")
        self.session.request.assert_not_called()

    def test_start_submits_only_one_job_and_file_id(self):
        operation = self.prefix + "1234567890abcdef"
        self.session.request.return_value = self._response({"name": operation})
        result = self.start("testfile_" + "a" * 22)
        self.assertEqual(result["status"], "submitted")
        method, url = self.session.request.call_args.args
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/jobs/scribeflow-transcribe:run"))
        payload = self.session.request.call_args.kwargs["json"]
        self.assertEqual(payload["overrides"]["taskCount"], 1)
        self.assertEqual(payload["overrides"]["containerOverrides"][0]["env"][0],
                         {"name": "SCRIBEFLOW_SOURCE_FILE_ID", "value": "testfile_" + "a" * 22})
        self.assertEqual(payload["overrides"]["containerOverrides"][0]["env"][1:], [
            {"name": "SCRIBEFLOW_LINKED_DRIVE_ENVELOPE", "value": "sealed"},
            {"name": "SCRIBEFLOW_REQUEST_OWNER", "value": "google-user"},
        ])
        self.assertEqual(payload["overrides"]["timeout"], "2700s")

    def test_markdown_format_is_forwarded_without_changing_txt_default(self):
        self.session.request.return_value = self._response({
            "name": self.prefix + "abcdef"
        })
        self.start("testfile_" + "a" * 22, output_format="md")
        env = self.session.request.call_args.kwargs["json"]["overrides"]["containerOverrides"][0]["env"]
        self.assertIn({"name": "SCRIBEFLOW_OUTPUT_FORMAT", "value": "md"}, env)
        with self.assertRaises(ValueError):
            self.start("testfile_" + "a" * 22, output_format="pdf")

    def test_explicit_transcript_id_is_scoped_to_replacement(self):
        self.session.request.return_value = self._response({
            "name": self.prefix + "1234567890abcdef"
        })
        audio_id = "audio_" + "a" * 25
        transcript_id = "text_" + "b" * 25
        with self.assertRaises(ValueError):
            self.start(audio_id, transcript_file_id=transcript_id)
        self.session.request.assert_not_called()
        self.start(audio_id, replace_existing=True, transcript_file_id=transcript_id)
        env = self.session.request.call_args.kwargs["json"]["overrides"]["containerOverrides"][0]["env"]
        self.assertIn({"name": "SCRIBEFLOW_TRANSCRIPT_FILE_ID", "value": transcript_id}, env)

    def test_optional_hints_are_sent_only_to_the_requested_job(self):
        operation = self.prefix + "1234567890abcdef"
        self.session.request.return_value = self._response({"name": operation})
        self.start(
            "testfile_" + "a" * 22,
            keywords=["  대인접수  ", "하나손해보험"],
            prompt="보험회사와 통화한 녹음",
        )
        env = self.session.request.call_args.kwargs["json"]["overrides"][
            "containerOverrides"
        ][0]["env"]
        self.assertEqual([item["name"] for item in env[:-2]], [
            "SCRIBEFLOW_SOURCE_FILE_ID",
            "SCRIBEFLOW_KEYWORDS_JSON", "SCRIBEFLOW_PROMPT",
        ])
        self.assertEqual(env[1]["value"], '["대인접수", "하나손해보험"]')
        self.assertEqual(env[2]["value"], "보험회사와 통화한 녹음")

    def test_auto_default_and_explicit_language_hint(self):
        self.session.request.return_value = self._response({
            "name": self.prefix + "abcdef"
        })
        self.start("testfile_" + "a" * 22)
        env = self.session.request.call_args.kwargs["json"]["overrides"][
            "containerOverrides"
        ][0]["env"]
        self.assertEqual(len(env), 3)
        self.start("testfile_" + "a" * 22, language="ko")
        env = self.session.request.call_args.kwargs["json"]["overrides"][
            "containerOverrides"
        ][0]["env"]
        self.assertEqual(env[-3], {
            "name": "SCRIBEFLOW_LANGUAGE", "value": "ko",
        })
        self.start("testfile_" + "a" * 22, language="zh-cn")
        with self.assertRaises(ValueError):
            self.start("testfile_" + "a" * 22, language="bad language")

    def test_invalid_transcription_hints_do_not_start_a_job(self):
        invalid = [
            ([""] , ""),
            (["a"] * 21, ""),
            (["a\nb"], ""),
            (["<tag>"], ""),
            ([123], ""),
            ([], "a" * 501),
        ]
        for keywords, prompt in invalid:
            with self.subTest(keywords=keywords, prompt=prompt):
                with self.assertRaises(ValueError):
                    self.start(
                        "testfile_" + "a" * 22,
                        keywords=keywords, prompt=prompt,
                    )
        self.session.request.assert_not_called()

    def test_status_does_not_allow_arbitrary_cloud_resources(self):
        with self.assertRaises(ValueError):
            self.api.status("projects/another/locations/asia-northeast3/operations/123")
        self.session.request.assert_not_called()

    def test_status_pending_then_completed(self):
        operation = self.prefix + "1234567890abcdef"
        execution = self.target.resource + "/executions/abc123"
        self.session.request.side_effect = [
            self._response({"done": False}),
            self._response({"done": True, "response": {"name": execution}}),
            self._response({"succeededCount": 1, "completionTime": "2026-11-13T00:00:00Z"}),
        ]
        self.assertEqual(self.api.status(operation)["status"], "running")
        self.assertEqual(self.api.status(operation)["status"], "completed")
        self.assertEqual(self.session.request.call_count, 3)

    def test_status_failure(self):
        operation = self.prefix + "1234567890abcdef"
        self.session.request.return_value = self._response({"done": True, "error": {"code": 13}})
        self.assertEqual(self.api.status(operation)["status"], "failed")

    def test_migrated_worker_submits_in_new_region_and_reads_old_execution(self):
        old = self.target
        new = CloudRunTarget(old.project, "us-central1", old.job)
        api = CloudRunJobs(new, session=self.session, previous_region=old.region)
        previous_operation = old.operation_prefix + "previous"
        previous_execution = old.resource + "/executions/previous"
        self.session.request.side_effect = [
            self._response({"name": new.operation_prefix + "current"}),
            self._response({"done": True, "response": {"name": previous_execution}}),
            self._response({"succeededCount": 1, "completionTime": "2026-11-21T00:00:00Z"}),
        ]

        self.assertEqual(api.start("testfile_" + "a" * 22, owner="google-user", drive_envelope="sealed")["operation"],
                         new.operation_prefix + "current")
        self.assertTrue(self.session.request.call_args.args[1].endswith(new.resource + ":run"))
        self.assertEqual(api.status(previous_operation)["status"], "completed")
        self.assertTrue(self.session.request.call_args.args[1].endswith(previous_execution))


if __name__ == "__main__":
    unittest.main()
