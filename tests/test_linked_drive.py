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

    def test_worker_uses_only_the_job_accounts_token_and_private_directory(self):
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

            def inspect_config(credential, file_id, private, **kwargs):
                self.assertEqual(credential.access_token, "google-b")
                self.assertEqual(Path(os.environ["SCRIBEFLOW_CACHE_DIR"]).parent, private)
                self.assertNotIn("RCLONE_CONFIG", os.environ)
                self.assertFalse((private / "rclone.conf").exists())
                self.assertEqual(file_id, FILE_ID)
                return {"ok": True, "private": private}

            with patch("scribeflow.cloud_worker.transcribe_drive", side_effect=inspect_config):
                result = run_job()
            self.assertTrue(result["ok"])
            self.assertFalse(result["private"].exists())
