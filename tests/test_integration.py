"""Cross-feature smoke tests with synthetic audio and a fake OpenAI client.

No real OpenAI API, Google Cloud resources, credentials or user files are used.
"""

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scribeflow.pipeline import transcribe
from scribeflow.provenance import source_fingerprint
from scribeflow.quality import compare_transcripts


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class IntegratedTranscriptionTests(unittest.TestCase):
    def synthetic_audio(self, directory: Path, suffix: str, duration: int = 12) -> Path:
        source = directory / ("test-recording" + suffix)
        codec = "libmp3lame" if suffix == ".mp3" else "aac"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i",
                f"sine=frequency=660:sample_rate=44100:duration={duration}",
                "-c:a", codec, "-b:a", "160k", str(source),
            ],
            capture_output=True, check=True, text=True,
        )
        return source

    def fake_client(self, captured):
        def create(*, file, **payload):
            captured.append({
                "name": Path(file.name).name,
                "body": file.read(),
                "request": payload,
            })
            return SimpleNamespace(text=f"전사 구간 {len(captured)}")
        return SimpleNamespace(
            audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)),
        )

    def test_small_original_skipped_on_repeat_and_evaluable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio = self.synthetic_audio(root, ".m4a", duration=5)
            original = audio.read_bytes()
            captured = []
            with patch("scribeflow.pipeline.cache_root", return_value=root / "cache"):
                with patch("scribeflow.engine.get_api_key", return_value="test"):
                    with patch("openai.OpenAI", return_value=self.fake_client(captured)):
                        created = transcribe(
                            str(audio), keywords=("대인접수",),
                            prompt="보험 통화",
                        )
                        reused = transcribe(str(audio))
            self.assertEqual(created["status"], "created")
            self.assertEqual(reused["status"], "skipped_existing")
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["body"], original)
            self.assertEqual(captured[0]["name"], audio.name)
            self.assertEqual(captured[0]["request"]["keywords"], ["대인접수"])
            self.assertEqual(captured[0]["request"]["prompt"], "보험 통화")
            self.assertEqual(audio.read_bytes(), original)
            text = audio.with_suffix(".txt").read_text(encoding="utf-8")
            self.assertEqual(source_fingerprint(text), hashlib.sha256(original).hexdigest())
            score = compare_transcripts("전사 구간 1", text, text)
            self.assertEqual(score["baseline"]["cer"], 0)
            self.assertEqual(score["candidate"]["cer"], 0)

    def test_large_stream_copy_chunks_publish_only_one_txt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio = self.synthetic_audio(root, ".mp3")
            original = audio.read_bytes()
            captured = []
            with patch("scribeflow.engine.MAX_UPLOAD_BYTES", 80_000):
                with patch("scribeflow.engine.CHUNK_TARGET_BYTES", 70_000):
                    with patch("scribeflow.pipeline.cache_root", return_value=root / "cache"):
                        with patch("scribeflow.engine.get_api_key", return_value="test"):
                            with patch("openai.OpenAI", return_value=self.fake_client(captured)):
                                result = transcribe(str(audio))
            self.assertEqual(result["status"], "created")
            self.assertGreater(len(captured), 1)
            self.assertTrue(all(
                0 < len(item["body"]) <= 70_000 for item in captured
            ))
            self.assertTrue(all(
                item["name"].endswith(".mp3") for item in captured
            ))
            self.assertEqual(audio.read_bytes(), original)
            text_file = audio.with_suffix(".txt")
            self.assertEqual(
                source_fingerprint(text_file.read_text(encoding="utf-8")),
                hashlib.sha256(original).hexdigest(),
            )
            # Every submitted chunk generated one recognized segment.
            transcript = text_file.read_text(encoding="utf-8")
            for index in range(1, len(captured) + 1):
                self.assertIn(f"전사 구간 {index}", transcript)
            self.assertEqual(
                {p.name for p in root.glob("*.txt")},
                {text_file.name},
            )


if __name__ == "__main__":
    unittest.main()
