import unittest
from unittest.mock import Mock

from scribeflow.cloud_run import CloudRunJobs, CloudRunTarget


class CloudRunTests(unittest.TestCase):
    def setUp(self):
        self.target = CloudRunTarget("sample-cloud-project", "asia-northeast3", "scribeflow-transcribe")
        self.session = Mock()
        self.api = CloudRunJobs(self.target, session=self.session)
        self.prefix = self.target.operation_prefix

    def _response(self, payload):
        response = Mock()
        response.json.return_value = payload
        return response

    def test_validate_file_before_cloud_api_request(self):
        with self.assertRaises(ValueError):
            self.api.start("../not-a-file-id")
        self.session.request.assert_not_called()

    def test_start_submits_only_one_job_and_file_id(self):
        operation = self.prefix + "1234567890abcdef"
        self.session.request.return_value = self._response({"name": operation})
        result = self.api.start("testfile_" + "a" * 22)
        self.assertEqual(result["status"], "submitted")
        method, url = self.session.request.call_args.args
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/jobs/scribeflow-transcribe:run"))
        payload = self.session.request.call_args.kwargs["json"]
        self.assertEqual(payload["overrides"]["taskCount"], 1)
        self.assertEqual(payload["overrides"]["containerOverrides"][0]["env"][0],
                         {"name": "SCRIBEFLOW_SOURCE_FILE_ID", "value": "testfile_" + "a" * 22})
        self.assertEqual(len(payload["overrides"]["containerOverrides"][0]["env"]), 1)

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


if __name__ == "__main__":
    unittest.main()
