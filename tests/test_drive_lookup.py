"""Drive ID metadata lookup tests. No real tokens, files, or network calls."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from scribeflow.drive_lookup import (
    DrivePathCandidate,
    _drive_metadata,
    _existing_access_token,
    candidate_from_id,
)


FILE_ID = "audio_" + "A" * 26
FOLDER_ID = "folder_" + "B" * 25
ROOT_ID = "root_" + "C" * 27
SCOPE_ID = "scope_" + "D" * 26
HIDDEN_PARENT = "hidden_" + "E" * 24


class DriveLookupTests(unittest.TestCase):
    def test_reads_private_token_without_exposing_rclone_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rclone.conf"
            future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            path.write_text(
                "[gdrive]\ntype = drive\n"
                + "token = "
                + json.dumps({
                    "access_token": "test-private-bearer",
                    "expiry": future,
                    "refresh_token": "private-refresh-should-not-leak",
                })
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"RCLONE_CONFIG": str(path)}):
                self.assertEqual(_existing_access_token(), "test-private-bearer")
            self.assertIn("private-refresh-should-not-leak", path.read_text())

    def test_expired_or_missing_private_token_uses_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rclone.conf"
            path.write_text(
                "[gdrive]\ntype = drive\n"
                + 'token = {"access_token":"expired","expiry":"2000-01-01T00:00:00Z"}',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"RCLONE_CONFIG": str(path)}):
                self.assertIsNone(_existing_access_token())
            with patch.dict(os.environ, {"RCLONE_CONFIG": ""}):
                self.assertIsNone(candidate_from_id(FILE_ID))

    def test_rest_call_is_read_only_and_targets_fixed_host(self):
        class FakeReply:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"id": FILE_ID, "name": "녹음.m4a"}).encode()

        with patch("scribeflow.drive_lookup.urlopen", return_value=FakeReply()) as call:
            self.assertEqual(_drive_metadata(FILE_ID, "test-bearer")["id"], FILE_ID)
        request = call.call_args.args[0]
        self.assertTrue(request.full_url.startswith(
            "https://www.googleapis.com/drive/v3/files/" + FILE_ID + "?"
        ))
        self.assertEqual(request.get_header("Authorization"), "Bearer test-bearer")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(call.call_args.kwargs["timeout"], 12)

    def test_file_under_my_drive_resolves_by_ancestors_only(self):
        values = {
            "root": {"id": ROOT_ID},
            FILE_ID: {
                "id": FILE_ID, "name": "녹음.m4a", "parents": [FOLDER_ID],
            },
            FOLDER_ID: {
                "id": FOLDER_ID, "name": "모임", "parents": [ROOT_ID],
            },
        }
        with patch("scribeflow.drive_lookup._existing_access_token", return_value="token"):
            with patch("scribeflow.drive_lookup._drive_metadata", side_effect=lambda key, _: values[key]) as get:
                candidate = candidate_from_id(FILE_ID)
        self.assertEqual(candidate, DrivePathCandidate("모임/녹음.m4a", False))
        self.assertEqual(get.call_count, 3)
        self.assertNotIn(ROOT_ID, [call.args[0] for call in get.call_args_list])

    def test_restricts_lookup_to_configured_root_folder(self):
        values = {
            FILE_ID: {"id": FILE_ID, "name": "lecture.mp3", "parents": [FOLDER_ID]},
            FOLDER_ID: {"id": FOLDER_ID, "name": "강의", "parents": [SCOPE_ID]},
        }
        with patch("scribeflow.drive_lookup._existing_access_token", return_value="token"):
            with patch("scribeflow.drive_lookup._drive_metadata", side_effect=lambda key, _: values[key]):
                self.assertEqual(
                    candidate_from_id(FILE_ID, SCOPE_ID),
                    DrivePathCandidate("강의/lecture.mp3", False),
                )
                self.assertIsNone(candidate_from_id(FILE_ID, ROOT_ID))

    def test_shared_folder_stops_at_inaccessible_parent(self):
        values = {
            "root": {"id": ROOT_ID},
            FILE_ID: {"id": FILE_ID, "name": "음성.m4a", "parents": [FOLDER_ID]},
            FOLDER_ID: {"id": FOLDER_ID, "name": "공유된폴더", "parents": [HIDDEN_PARENT]},
        }
        def fetch(key, token):
            if key == HIDDEN_PARENT:
                raise HTTPError("https://www.googleapis.com", 403, "Forbidden", {}, None)
            return values[key]
        with patch("scribeflow.drive_lookup._existing_access_token", return_value="token"):
            with patch("scribeflow.drive_lookup._drive_metadata", side_effect=fetch):
                self.assertEqual(
                    candidate_from_id(FILE_ID),
                    DrivePathCandidate("공유된폴더/음성.m4a", True),
                )

    def test_shared_folder_not_used_outside_explicit_scope(self):
        values = {
            FILE_ID: {"id": FILE_ID, "name": "음성.m4a", "parents": [HIDDEN_PARENT]},
        }
        def fetch(key, token):
            if key == HIDDEN_PARENT:
                raise HTTPError("https://www.googleapis.com", 404, "Not found", {}, None)
            return values[key]
        with patch("scribeflow.drive_lookup._existing_access_token", return_value="token"):
            with patch("scribeflow.drive_lookup._drive_metadata", side_effect=fetch):
                self.assertIsNone(candidate_from_id(FILE_ID, SCOPE_ID))

    def test_names_with_path_separators_and_parent_cycles_fall_back(self):
        with patch("scribeflow.drive_lookup._existing_access_token", return_value="token"):
            with patch("scribeflow.drive_lookup._drive_metadata", side_effect=lambda key, _: {
                "root": {"id": ROOT_ID},
                FILE_ID: {
                    "id": FILE_ID, "name": "../danger.m4a", "parents": [FOLDER_ID],
                },
            }[key]):
                self.assertIsNone(candidate_from_id(FILE_ID))

            loop = {
                "root": {"id": ROOT_ID},
                FILE_ID: {"id": FILE_ID, "name": "ok.m4a", "parents": [FOLDER_ID]},
                FOLDER_ID: {
                    "id": FOLDER_ID, "name": "folder", "parents": [FILE_ID],
                },
            }
            with patch("scribeflow.drive_lookup._drive_metadata", side_effect=lambda key, _: loop[key]):
                self.assertIsNone(candidate_from_id(FILE_ID))


if __name__ == "__main__":
    unittest.main()
