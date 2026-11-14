import errno
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scribeflow.engine import (
    MAX_UPLOAD_BYTES, auto_chunk_seconds, clock, plan_chunks,
    plan_audio_chunks, prepare_stream_copy_chunks, publish_prepared_chunk,
    transcribe_file,
)
from scribeflow.credentials import get_api_key


class ChunkTests(unittest.TestCase):
    def test_silence_cut_prevents_overlap_and_preserves_order(self):
        with patch("scribeflow.engine.detect_silence_cut", return_value=96.0):
            chunks = plan_audio_chunks(Path("test.wav"), 175, 100, 3)
        self.assertEqual([(c.start, c.end) for c in chunks], [(0, 96), (96, 175)])

    def test_active_speech_keeps_overlap(self):
        with patch("scribeflow.engine.detect_silence_cut", return_value=None):
            chunks = plan_audio_chunks(Path("test.wav"), 175, 100, 3)
        self.assertEqual([(c.start, c.end) for c in chunks], [(0, 100), (97, 175)])

    def test_no_chunk_exceeds_budget(self):
        with patch("scribeflow.engine.detect_silence_cut", return_value=None):
            chunks = plan_audio_chunks(Path("test.wav"), 201, 100, 3)
        self.assertTrue(all(c.duration <= 100 for c in chunks))

    def test_auto_plan_keeps_recording_in_one_chunk(self):
        self.assertEqual(len(plan_chunks(1426.09, auto_chunk_seconds(1426.09, 8_000_000), 3)), 1)

    def test_short_file(self):
        chunks = plan_chunks(25)
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0].start, chunks[0].end), (0, 25))

    def test_overlap_and_last_chunk(self):
        chunks = plan_chunks(1230, 600, 3)
        self.assertEqual(chunks[0].start, 0)
        self.assertEqual(chunks[-1].end, 1230)
        for previous, current in zip(chunks, chunks[1:]):
            self.assertAlmostEqual(previous.end - current.start, 3)

    def test_no_tiny_tail(self):
        chunks = plan_chunks(603, 600, 3)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].end, 603)

    def test_invalid_config(self):
        with self.assertRaises(ValueError):
            plan_chunks(100, 0, 0)
        with self.assertRaises(ValueError):
            plan_chunks(100, 20, 6)

    def test_clock(self):
        self.assertEqual(clock(3661), "01:01:01")


class CredentialsTests(unittest.TestCase):
    def test_explicit_env_has_priority(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-for-test"}):
            self.assertEqual(get_api_key(), "fake-for-test")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class LosslessAudioTests(unittest.TestCase):
    def make_audio(self, directory: Path, suffix: str, duration: int = 12) -> Path:
        source = directory / ("recording" + suffix)
        codec = "libmp3lame" if suffix == ".mp3" else "aac"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i",
                f"sine=frequency=440:sample_rate=44100:duration={duration}",
                "-c:a", codec, "-b:a", "160k", str(source),
            ],
            check=True, capture_output=True, text=True,
        )
        return source

    def fake_client(self, captured: list[tuple[str, bytes]]):
        def create(*, file, **payload):
            captured.append((Path(file.name).suffix, file.read()))
            self.assertEqual(payload["model"], "gpt-transcribe")
            return SimpleNamespace(text="테스트 전사 결과.")
        return SimpleNamespace(
            audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create))
        )

    def test_direct_upload_sends_identical_original_m4a(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_audio(root, ".m4a")
            original = source.read_bytes()
            captured = []
            with patch("scribeflow.engine.get_api_key", return_value="test"):
                with patch("openai.OpenAI", return_value=self.fake_client(captured)):
                    transcribe_file(source, root / "output")
            self.assertEqual(captured, [(".m4a", original)])
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(list((root / "output" / "audio_chunks").iterdir()))
            manifest = json.loads((root / "output" / "manifest.json").read_text())
            self.assertEqual(manifest["encoding"], "original")
            self.assertEqual(len(manifest["chunks"]), 1)

    def test_auto_language_omits_api_language_hint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_audio(root, ".mp3", duration=3)
            payloads = []
            def create(*, file, **payload):
                payloads.append(payload)
                return SimpleNamespace(text="hello")
            client = SimpleNamespace(
                audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create))
            )
            with patch("scribeflow.engine.get_api_key", return_value="test"), \
                 patch("openai.OpenAI", return_value=client):
                transcribe_file(source, root / "auto", language="auto")
            self.assertEqual(payloads, [{"model": "gpt-transcribe"}])

    def test_multitrack_split_rejects_without_dropping_second_track(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "two-tracks.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
                "-map", "0:a", "-map", "1:a",
                "-c:a", "aac", str(source),
            ], check=True, capture_output=True)
            with patch("scribeflow.engine.get_api_key", return_value="test"), \
                 patch("scribeflow.engine.MAX_UPLOAD_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "다중 오디오 트랙"):
                    transcribe_file(source, root / "output")
            self.assertFalse((root / "output").exists())

    def test_large_mp3_and_m4a_use_copy_only_and_fit_byte_limit(self):
        for suffix in (".mp3", ".m4a"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = self.make_audio(root, suffix)
                original = source.read_bytes()
                captured = []
                with patch("scribeflow.engine.MAX_UPLOAD_BYTES", 80_000):
                    with patch("scribeflow.engine.CHUNK_TARGET_BYTES", 70_000):
                        with patch("scribeflow.engine.get_api_key", return_value="test"):
                            with patch("openai.OpenAI", return_value=self.fake_client(captured)):
                                transcribe_file(source, root / "output")
                self.assertGreater(len(captured), 1)
                self.assertEqual(source.read_bytes(), original)
                self.assertTrue(all(ext == suffix for ext, _ in captured))
                self.assertTrue(all(0 < len(blob) <= 70_000 for _, blob in captured))
                manifest = json.loads((root / "output" / "manifest.json").read_text())
                self.assertEqual(manifest["encoding"], "stream-copy")
                self.assertEqual(len(manifest["chunks"]), len(captured))
                self.assertTrue(all(
                    chunk["end"] > chunk["start"] for chunk in manifest["chunks"]
                ))

    def test_lossless_split_preserves_every_encoded_packet(self):
        """Boundary seeks must neither repeat nor lose encoded audio packets."""
        formats = (
            (".mp3", "libmp3lame"), (".m4a", "aac"),
            (".ogg", "libopus"), (".webm", "libopus"),
        )

        def packet_hashes(path: Path) -> list[str]:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "a:0",
                    "-show_packets", "-show_data_hash", "sha256",
                    "-show_entries", "packet=data_hash", "-of", "json", str(path),
                ],
                check=True, capture_output=True, text=True,
            )
            return [
                packet["data_hash"]
                for packet in json.loads(result.stdout)["packets"]
            ]

        for suffix, codec in formats:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / ("noise" + suffix)
                subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i",
                        "anoisesrc=c=pink:sample_rate=48000:duration=12",
                        "-c:a", codec, str(source),
                    ],
                    check=True, capture_output=True, text=True,
                )
                original_bytes = source.read_bytes()
                original_packets = packet_hashes(source)
                chunks = prepare_stream_copy_chunks(
                    source, 12.0, 4, 0, root / "chunks",
                )
                self.assertEqual(len(chunks), 3)
                actual_packets = [
                    packet
                    for chunk in chunks
                    for packet in packet_hashes(
                        root / "chunks" / f"{chunk.name}{suffix}"
                    )
                ]
                self.assertEqual(actual_packets, original_packets)
                self.assertEqual(source.read_bytes(), original_bytes)

    def test_interrupted_preflight_staging_is_resumable(self):
        """A failure after manifest creation must not force a paid restart."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_audio(root, ".mp3")
            original = source.read_bytes()
            output = root / "output"
            captured = []
            client = self.fake_client(captured)
            with patch("scribeflow.engine.MAX_UPLOAD_BYTES", 80_000):
                with patch("scribeflow.engine.CHUNK_TARGET_BYTES", 70_000):
                    with patch("scribeflow.engine.get_api_key", return_value="test"):
                        with patch("openai.OpenAI", return_value=client):
                            with patch(
                                "scribeflow.engine.publish_prepared_chunk",
                                side_effect=OSError("simulated interrupted copy"),
                            ):
                                with self.assertRaisesRegex(OSError, "interrupted"):
                                    transcribe_file(source, output)
                            self.assertTrue((output / "manifest.json").is_file())
                            self.assertEqual(captured, [])
                            transcribe_file(source, output)
            self.assertGreater(len(captured), 1)
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(all(
                0 < len(payload) <= 70_000 for _, payload in captured
            ))

    def test_cross_filesystem_publish_uses_atomic_copy_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "validated.mp3"
            destination = root / "published.mp3"
            source.write_bytes(b"validated encoded audio")
            original_replace = Path.replace
            injected = {"raised": False}

            def fail_rename_once(path, target):
                if path == source and not injected["raised"]:
                    injected["raised"] = True
                    raise OSError(errno.EXDEV, "cross-filesystem rename")
                return original_replace(path, target)

            with patch.object(Path, "replace", fail_rename_once):
                publish_prepared_chunk(source, destination)

            self.assertTrue(injected["raised"])
            self.assertEqual(destination.read_bytes(), b"validated encoded audio")
            self.assertFalse((root / "published.mp3.part").exists())

    def test_unsupported_audio_is_not_lossily_converted(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "recording.wma"
            source.write_bytes(b"not audio")
            with patch("scribeflow.engine.get_api_key", return_value="test"):
                with patch("scribeflow.engine.inspect_audio", return_value={
                    "size_bytes": 42, "duration_seconds": 10,
                }):
                    with self.assertRaisesRegex(ValueError, "지원하지 않는 파일 형식"):
                        transcribe_file(source, Path(folder) / "output")


if __name__ == "__main__":
    unittest.main()
