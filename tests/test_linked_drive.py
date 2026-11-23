import json
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from cryptography.fernet import Fernet

from scribeflow.cloud_worker import run_job
from scribeflow.linked_drive import DRIVE_SCOPE, DriveCredential, search_files


FILE_ID = "audio_" + "a" * 26
FOLDER_ID = "folder_" + "b" * 20
DRIVE_ID = "drive_" + "c" * 20


class LinkedDriveTests(unittest.TestCase):
    def test_search_uses_each_connections_bearer_and_google_pagination(self):
        for token, drive_id in (("google-a", ""), ("google-b", DRIVE_ID)):
            auth = SimpleNamespace(scopes=[DRIVE_SCOPE], token=token, expires_at=time.time() + 3600)
            credential = DriveCredential.from_connection(auth)
            with patch("scribeflow.linked_drive.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.read.return_value = (
                    '{"files":[],"incompleteSearch":true,"nextPageToken":"more"}'
                )
                result = search_files(credential, "Owner's", "current", drive_id)
            request = urlopen.call_args.args[0]
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(request.get_header("Authorization"), f"Bearer {token}")
            self.assertEqual(query["q"], ["trashed = false and name contains 'Owner\\'s'"])
            self.assertEqual(query["pageToken"], ["current"])
            self.assertEqual(query["corpora"], ["drive" if drive_id else "allDrives"])
            self.assertEqual(result["nextPageToken"], "more")
            self.assertTrue(result["incompleteSearch"])

    def test_worker_uses_only_the_job_accounts_private_config_and_folder(self):
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test",
            "SCRIBEFLOW_DRIVE_JOB_KEY": key,
            "SCRIBEFLOW_SOURCE_FILE_ID": FILE_ID,
        }, clear=True):
            credential = DriveCredential("google-b", int(time.time()) + 3600)
            os.environ["SCRIBEFLOW_LINKED_DRIVE_ENVELOPE"] = envelope = credential.seal(FILE_ID)
            self.assertNotIn("google-b", envelope)
            with self.assertRaises(PermissionError):
                DriveCredential.open(envelope, "another_" + "b" * 26)

            def inspect_config(file_id, **kwargs):
                path = Path(os.environ["RCLONE_CONFIG"])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                configuration = path.read_text()
                token_line = next(line for line in configuration.splitlines() if line.startswith("token = "))
                self.assertEqual(json.loads(token_line.removeprefix("token = "))["access_token"], "google-b")
                self.assertIn(f"team_drive = {DRIVE_ID}", configuration)
                self.assertEqual(os.environ["SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID"], FOLDER_ID)
                self.assertEqual(file_id, FILE_ID)
                return {"ok": True, "config_path": path}

            with patch("scribeflow.cloud_worker.check_file", return_value={
                "can_download": True, "can_save_transcript": True,
                "drive_id": DRIVE_ID, "parent_id": FOLDER_ID,
            }), patch("scribeflow.cloud_worker.transcribe", side_effect=inspect_config):
                result = run_job()
            self.assertTrue(result["ok"])
            self.assertFalse(result["config_path"].exists())
