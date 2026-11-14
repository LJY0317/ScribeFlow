"""Storage and billing guardrails using only synthetic inputs and fake API responses.

These tests never call a real OpenAI API, Google Cloud, or user Google Drive.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scribeflow.cache import cache_stats, clean_cache, retain_failed_cache
from scribeflow.engine import transcribe_file
from scribeflow.pipeline import NoSpeechDetectedError, transcribe
from scribeflow.provenance import source_fingerprint


def fake_engine(source, output, *, texts=("반드시 남을 발언",), **kwargs):
    output.mkdir(parents=True, exist_ok=True)
    transcripts = output / "transcripts"
    transcripts.mkdir(exist_ok=True)
    (output / "audio_chunks").mkdir(exist_ok=True)
    (output / "audio_chunks" / "intermediate.mp3").write_bytes(b"temporary packet data")
    manifest = {
        "chunks": [
            {"index": n, "start": n - 1, "end": n}
            for n in range(1, len(texts) + 1)
        ]
    }
    (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for n, segment in enumerate(texts, 1):
        (transcripts / f"{n:04d}.txt").write_text(segment, encoding="utf-8")
    return output


class PipelineLifecycleTests(unittest.TestCase):
    def test_all_blank_api_segments_never_publish_a_fingerprint_only_txt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.m4a"
            source.write_bytes(b"untouched audio")
            cache = root / "cache"
            with patch("scribeflow.pipeline.cache_root", return_value=cache):
                with patch("scribeflow.pipeline.transcribe_file", side_effect=lambda s, p, **kw: fake_engine(
                    s, p, texts=(" \n", "\t"), **kw,
                )):
                    with self.assertRaisesRegex(NoSpeechDetectedError, "발언이 없습니다"):
                        transcribe(str(source))
            self.assertEqual(source.read_bytes(), b"untouched audio")
            self.assertFalse(source.with_suffix(".txt").exists())
            # Failed local jobs are reusable for 24h by default.
            self.assertEqual(cache_stats(cache)["jobs"], 1)

    def test_discard_mode_removes_partial_failure_artifacts_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.m4a"
            source.write_bytes(b"untouched audio")
            cache = root / "cache"

            def fail_after_partial(s, p, **kw):
                fake_engine(s, p, **kw)
                raise RuntimeError("synthetic API interruption")

            with patch.dict(os.environ, {"SCRIBEFLOW_FAILED_CACHE_MODE": "discard"}):
                self.assertFalse(retain_failed_cache())
                with patch("scribeflow.pipeline.cache_root", return_value=cache):
                    with patch(
                        "scribeflow.pipeline.transcribe_file",
                        side_effect=fail_after_partial,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "interruption"):
                            transcribe(str(source))
            self.assertEqual(cache_stats(cache), {"jobs": 0, "bytes": 0})
            self.assertFalse(source.with_suffix(".txt").exists())
            self.assertEqual(source.read_bytes(), b"untouched audio")

    def test_default_mode_keeps_failed_segments_to_avoid_duplicate_api_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.m4a"
            source.write_bytes(b"audio")
            cache = root / "cache"
            with patch.dict(os.environ, {"SCRIBEFLOW_FAILED_CACHE_MODE": "retry"}):
                self.assertTrue(retain_failed_cache())
                with patch("scribeflow.pipeline.cache_root", return_value=cache):
                    with patch("scribeflow.pipeline.transcribe_file", side_effect=lambda s, p, **kw: fake_engine(
                        s, p, texts=("\n",), **kw,
                    )):
                        with self.assertRaises(ValueError):
                            transcribe(str(source))
            self.assertEqual(cache_stats(cache)["jobs"], 1)
            self.assertFalse(source.with_suffix(".txt").exists())

    def test_invalid_cache_mode_rejected_before_transcription_or_download(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "recording.m4a"
            source.write_bytes(b"audio")
            with patch.dict(os.environ, {"SCRIBEFLOW_FAILED_CACHE_MODE": "unsafe"}):
                with patch("scribeflow.pipeline.transcribe_file") as engine:
                    with self.assertRaisesRegex(ValueError, "FAILED_CACHE_MODE"):
                        transcribe(str(source))
                    engine.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [source])

    def test_mixed_silent_chunks_publish_only_recognized_words(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.m4a"
            source.write_bytes(b"audio")
            cache = root / "cache"
            with patch("scribeflow.pipeline.cache_root", return_value=cache):
                with patch("scribeflow.pipeline.transcribe_file", side_effect=lambda s, p, **kw: fake_engine(
                    s, p, texts=("   ", "대인 접수입니다.", "\n"), **kw,
                )):
                    result = transcribe(str(source))
            self.assertEqual(result["status"], "created")
            text = source.with_suffix(".txt").read_text(encoding="utf-8")
            self.assertIn("대인 접수입니다.", text)
            self.assertNotIn("intermediate", text)
            self.assertIsNotNone(source_fingerprint(text))
            self.assertEqual(cache_stats(cache), {"jobs": 0, "bytes": 0})
            self.assertEqual(
                sorted(path.name for path in root.iterdir() if path.is_file()),
                ["recording.m4a", "recording.txt"],
            )

    def test_stale_published_cache_is_reaped_even_when_recent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.m4a"
            source.write_bytes(b"audio")
            cache = root / "cache"
            original_rmtree = shutil.rmtree

            def simulate_cleanup_error(path, *args, **kwargs):
                if Path(path).parent == cache / "jobs":
                    raise OSError("disk temporarily unavailable")
                return original_rmtree(path, *args, **kwargs)

            with patch("scribeflow.pipeline.cache_root", return_value=cache):
                with patch("scribeflow.pipeline.transcribe_file", side_effect=fake_engine):
                    with patch("scribeflow.pipeline.shutil.rmtree", side_effect=simulate_cleanup_error):
                        self.assertEqual(transcribe(str(source))["status"], "created")
            self.assertTrue(source.with_suffix(".txt").exists())
            self.assertEqual(cache_stats(cache)["jobs"], 1)
            self.assertEqual(clean_cache(cache)["jobs"], 0)
            self.assertEqual(source.read_bytes(), b"audio")


class CloudTemporaryStorageTests(unittest.TestCase):
    def test_cloud_job_cleans_private_audio_and_credentials_on_exit(self):
        from scribeflow.cloud_worker import run_job

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "rclone.conf"
            secret.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
            original_temporary_directory = tempfile.TemporaryDirectory

            for fail in (False, True):
                with self.subTest(fail=fail):
                    created = []

                    def capture_temporary_dir(*args, **kwargs):
                        instance = original_temporary_directory(*args, **kwargs)
                        created.append(Path(instance.name))
                        return instance

                    def simulate_transcription(file_id):
                        cloud_cache = Path(os.environ["SCRIBEFLOW_CACHE_DIR"])
                        cloud_cache.mkdir(parents=True, exist_ok=True)
                        (cloud_cache / "fake-segment.mp3").write_bytes(b"temporary bytes")
                        if fail:
                            raise RuntimeError("synthetic failed upload")
                        return {"ok": True, "status": "created"}

                    with patch.dict(os.environ, {
                        "OPENAI_API_KEY": "test-only",
                        "SCRIBEFLOW_SOURCE_FILE_ID": "file_" + "a" * 26,
                        "SCRIBEFLOW_RCLONE_CONFIG_SECRET": str(secret),
                        "SCRIBEFLOW_KEYWORDS_JSON": "[]",
                        "SCRIBEFLOW_PROMPT": "",
                    }):
                        with patch(
                            "scribeflow.cloud_worker.tempfile.TemporaryDirectory",
                            side_effect=capture_temporary_dir,
                        ):
                            with patch(
                                "scribeflow.cloud_worker.transcribe",
                                side_effect=simulate_transcription,
                            ):
                                if fail:
                                    with self.assertRaisesRegex(RuntimeError, "upload"):
                                        run_job()
                                else:
                                    self.assertEqual(run_job()["status"], "created")
                    self.assertEqual(len(created), 1)
                    self.assertFalse(created[0].exists())
                    self.assertTrue(secret.exists())


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class AudioChunkCleanupTests(unittest.TestCase):
    def test_completed_chunks_are_discarded_without_touching_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "recording.mp3"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i",
                    "sine=frequency=510:sample_rate=44100:duration=12",
                    "-c:a", "libmp3lame", "-b:a", "160k", str(audio),
                ],
                check=True, capture_output=True, text=True,
            )
            original = audio.read_bytes()
            submitted = []

            def create(*, file, **kwargs):
                submitted.append(file.read())
                return SimpleNamespace(text=f"segment {len(submitted)}")

            client = SimpleNamespace(
                audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create))
            )
            with patch("scribeflow.engine.MAX_UPLOAD_BYTES", 80_000):
                with patch("scribeflow.engine.CHUNK_TARGET_BYTES", 70_000):
                    with patch("scribeflow.engine.get_api_key", return_value="test"):
                        with patch("openai.OpenAI", return_value=client):
                            transcribe_file(audio, root / "job")
            self.assertGreater(len(submitted), 1)
            self.assertEqual(audio.read_bytes(), original)
            self.assertEqual(list((root / "job" / "audio_chunks").iterdir()), [])
            self.assertEqual(
                len(list((root / "job" / "transcripts").glob("*.txt"))),
                len(submitted),
            )


if __name__ == "__main__":
    unittest.main()
