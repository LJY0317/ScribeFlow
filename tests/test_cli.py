import contextlib
import io
import json
import unittest
from unittest.mock import patch

from scribeflow.cli import main


class CliTests(unittest.TestCase):
    def test_scan_json_for_agents(self):
        result = {"untranscribed_count": 2, "untranscribed": []}
        buffer = io.StringIO()
        with patch("scribeflow.cli.scan_drive", return_value=result):
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(main(["scan", "gdrive:", "--recursive", "--json"]), 0)
        self.assertEqual(json.loads(buffer.getvalue()), result)

    def test_json_result_is_from_pipeline(self):
        expected = {
            "ok": True,
            "transcript": "/tmp/recording.txt",
            "status": "created",
        }
        buffer = io.StringIO()
        with patch("scribeflow.cli.transcribe", return_value=expected) as job:
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(main(["transcribe", "/tmp/recording.m4a", "--json"]), 0)
        self.assertEqual(json.loads(buffer.getvalue()), expected)
        job.assert_called_once()


if __name__ == "__main__":
    unittest.main()
