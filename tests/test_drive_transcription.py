"""Linked-account Drive ID workflow, with no real API or user data."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from scribeflow.drive_transcription import transcribe_drive, DriveTranscriptionError
from scribeflow.linked_drive import DriveCredential
from scribeflow.provenance import stamp_transcript


class DriveTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.private = tempfile.TemporaryDirectory()
        self.addCleanup(self.private.cleanup)
        self.root = Path(self.private.name)
        self.audio_id = "audio_" + "a" * 25
        self.text_id = "text_" + "b" * 25
        self.folder = "folder_" + "c" * 25
        self.audio = b"synthetic recording"
        self.sha = hashlib.sha256(self.audio).hexdigest()
        self.credential = DriveCredential("test", 0)
        self.source = {"id": self.audio_id, "name": "sample.m4a", "parents": [self.folder],
                       "sha256Checksum": self.sha, "capabilities": {"canDownload": True}}
        self.old_text = stamp_transcript("Old speech", self.sha).encode()
        self.new_text = stamp_transcript("New speech", self.sha).encode()
        self.target = {"id": self.text_id, "name": "sample.txt", "mimeType": "text/plain",
                       "sha256Checksum": hashlib.sha256(self.old_text).hexdigest(),
                       "capabilities": {"canEdit": True}}
        for name, kwargs in {
            "file_metadata": {"side_effect": lambda cred, file_id: dict(self.source if file_id == self.audio_id else self.target)},
            "download_file": {"side_effect": lambda cred, file_id, destination: destination.write_bytes(self.audio)},
            "sibling_transcripts": {"return_value": []},
            "can_add_to_folder": {"return_value": True},
            "read_transcript": {"return_value": self.old_text},
            "transcribe": {"side_effect": self.fake_engine},
            "create_transcript": {"return_value": {"id": self.text_id}},
            "update_transcript": {"return_value": {"id": self.text_id}},
            "find_transcripts_by_name": {"return_value": []},
        }.items():
            p = patch("scribeflow.drive_transcription." + name, **kwargs)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)

    def fake_engine(self, source, output_format, **kwargs):
        self.assertEqual(Path(source).read_bytes(), self.audio)
        self.assertEqual(kwargs["source_filename"], "sample.m4a")
        self.assertFalse(kwargs["retain_chunks"])
        output = Path(source).with_suffix("." + output_format)
        output.write_bytes(self.new_text)
        return {"ok": True, "transcript": str(output), "model": "gpt-transcribe"}

    def run_drive(self, **kwargs):
        return transcribe_drive(self.credential, self.audio_id, self.root, **kwargs)

    def test_new_markdown_is_uploaded_by_parent_id_with_actual_returned_id(self):
        result = self.run_drive(output_format="md")
        self.download_file.assert_called_once_with(self.credential, self.audio_id, self.root / "audio.m4a")
        self.create_transcript.assert_called_once_with(self.credential, self.folder, "sample.md", self.new_text, "text/markdown")
        self.assertEqual(result["transcript_file_id"], self.text_id)
        self.assertEqual(result["output_format"], "md")

    def test_verified_other_format_is_reused_without_paid_work(self):
        self.sibling_transcripts.return_value = [self.target]
        result = self.run_drive(output_format="md")
        self.assertEqual(result["status"], "skipped_existing")
        self.assertEqual(result["output_format"], "txt")
        self.assertEqual(result["transcript_file_id"], self.text_id)
        self.download_file.assert_not_called()
        self.transcribe.assert_not_called()
        self.create_transcript.assert_not_called()

    def test_explicit_replacement_works_without_accessible_source_parent(self):
        self.source.pop("parents")
        self.target["name"] = "renamed.md"
        self.target["mimeType"] = "text/markdown"
        result = self.run_drive(output_format="md", replace_existing=True, transcript_file_id=self.text_id)
        self.assertEqual(result["status"], "replaced")
        self.sibling_transcripts.assert_not_called()
        self.can_add_to_folder.assert_not_called()
        self.update_transcript.assert_called_once_with(self.text_id, "test", self.new_text, content_type="text/markdown")

    def test_same_name_conflict_or_temporary_candidate_error_never_bills(self):
        self.sibling_transcripts.return_value = [self.target]
        self.read_transcript.return_value = b"unrelated user file"
        with self.assertRaises(DriveTranscriptionError) as failure:
            self.run_drive()
        self.assertEqual(failure.exception.code, "destination_conflict")
        self.read_transcript.side_effect = HTTPError("https://example.invalid", 503, "unavailable", None, None)
        with self.assertRaises(HTTPError):
            self.run_drive(replace_existing=True)
        self.transcribe.assert_not_called()

    def test_source_change_after_transcription_prevents_publication(self):
        self.file_metadata.side_effect = [self.source, {**self.source, "sha256Checksum": "f" * 64}]
        with self.assertRaises(DriveTranscriptionError) as failure:
            self.run_drive()
        self.assertEqual(failure.exception.code, "source_changed")
        self.create_transcript.assert_not_called()

    def test_changed_replacement_is_not_overwritten(self):
        self.file_metadata.side_effect = [self.source, self.target, self.source,
                                          {**self.target, "sha256Checksum": "f" * 64}]
        with self.assertRaises(DriveTranscriptionError) as failure:
            self.run_drive(replace_existing=True, transcript_file_id=self.text_id)
        self.assertEqual(failure.exception.code, "destination_changed")
        self.update_transcript.assert_not_called()
