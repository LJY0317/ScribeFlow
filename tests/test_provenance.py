import hashlib
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scribeflow import __version__
from scribeflow.pipeline import transcribe
from scribeflow.provenance import (
    TranscriptMetadata,
    can_upgrade_transcript,
    source_fingerprint,
    stamp_transcript,
    strip_transcript_footer,
)


class ProvenanceTests(unittest.TestCase):
    def test_metadata_footer_preserves_legacy_last_line_and_spoken_text(self):
        original = "안녕하세요.\n"
        sha = "a" * 64
        metadata = TranscriptMetadata(
            source_filename='녹음 "A" -->\n.m4a',
            source_size_bytes=6_430_288,
            duration_seconds=1800.125,
            provider="OpenAI",
            model_id="gpt-transcribe",
            completed_at=datetime(2026, 11, 21, 5, 30, tzinfo=timezone.utc),
            detected_languages=("ko", "en"),
        )
        stamped = stamp_transcript(original, sha, metadata)
        self.assertEqual(stamped.splitlines(), [
            "안녕하세요.",
            "",
            r'<!-- ScribeFlow Metadata: Source: "녹음 \"A\" --\u003e\n.m4a"'
            " | Duration: 0:30:00.125000 | Size: 6,430,288 bytes | Detected languages: ko, en"
            f" | Transcript SHA256: {hashlib.sha256((original + chr(10)).encode()).hexdigest()}"
            " | Provider: OpenAI | Model ID: gpt-transcribe"
            " | Snapshot: 미공개 | Transcribed at: 2026-11-21T05:30:00+00:00"
            f" | ScribeFlow version: {__version__} -->",
            f"<!-- ScribeFlow SHA256: {sha} -->",
        ])
        self.assertEqual(source_fingerprint(stamped), sha)
        self.assertEqual(strip_transcript_footer(stamped), original + "\n")
        self.assertEqual(stamp_transcript(stamped, sha, metadata), stamped)

    def test_upgrade_policy_only_accepts_known_provider_model_pairs_and_untouched_body(self):
        sha = "a" * 64
        for model, provider, expected in (
            ("whisper-1", "OpenAI", True),
            ("gpt-4o-transcribe", "OpenAI", True),
            ("gpt-4o-mini-transcribe-2025-12-15", "OpenAI", True),
            ("gpt-transcribe", "OpenAI", False),
            ("gpt-4o-transcribe-diarize", "OpenAI", False),
            ("unknown-new-model", "OpenAI", False),
            ("whisper-1", "Other", False),
        ):
            with self.subTest(model=model, provider=provider):
                stamped = stamp_transcript("speech", sha, TranscriptMetadata(
                    "audio.m4a", 10, 1, provider, model, datetime.now(timezone.utc)))
                self.assertEqual(can_upgrade_transcript(stamped, "gpt-transcribe"), expected)
                self.assertFalse(can_upgrade_transcript(stamped, "unknown-new-model"))
                self.assertFalse(can_upgrade_transcript(stamped.replace("speech", "edited"), "gpt-transcribe"))
                legacy = re.sub(r" \| Transcript SHA256: [0-9a-f]{64}", "", stamped)
                self.assertFalse(can_upgrade_transcript(legacy, "gpt-transcribe"))
        self.assertFalse(can_upgrade_transcript(stamp_transcript("speech", sha), "gpt-transcribe"))

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
