import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scribeflow.pipeline import transcribe
from scribeflow.provenance import source_fingerprint, stamp_transcript


class ProvenanceTests(unittest.TestCase):
    def test_source_fingerprint_is_last_line_and_idempotent(self):
        sha = hashlib.sha256(b"source").hexdigest()
        original = "안녕하세요.\n"
        stamped = stamp_transcript(original, sha)
        self.assertEqual(stamped, original + "\n<!-- ScribeFlow SHA256: " + sha + " -->\n")
        self.assertEqual(source_fingerprint(stamped), sha)
        self.assertEqual(stamp_transcript(stamped, sha), stamped)
        self.assertIsNone(source_fingerprint(stamped + "다른 글\n"))
        with self.assertRaises(ValueError):
            stamp_transcript(stamped, "a" * 64)

    def test_unstamped_existing_is_never_retranscribed(self):
        with tempfile.TemporaryDirectory() as d:
            audio = Path(d) / "recording.m4a"
            audio.write_bytes(b"source")
            txt = Path(d) / "recording.txt"
            txt.write_text("기존 내용\n", encoding="utf-8")
            with patch("scribeflow.pipeline.cache_root", return_value=Path(d) / "cache"):
                with patch("scribeflow.pipeline.transcribe_file") as engine:
                    outcome = transcribe(str(audio))
            self.assertEqual(outcome["status"], "unverified_existing")
            self.assertFalse(outcome["ok"])
            self.assertEqual(txt.read_text(encoding="utf-8"), "기존 내용\n")
            engine.assert_not_called()

    def test_wrong_sha_is_not_a_verified_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            audio = Path(d) / "recording.m4a"
            audio.write_bytes(b"actual-source")
            txt = Path(d) / "recording.txt"
            txt.write_text(stamp_transcript("기존 내용\n", "a" * 64))
            with patch("scribeflow.pipeline.cache_root", return_value=Path(d) / "cache"):
                result = transcribe(str(audio))
            self.assertEqual(result["status"], "source_mismatch")
            self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
