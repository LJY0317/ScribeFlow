import unittest
from unittest.mock import patch
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scribeflow.engine import auto_chunk_seconds, clock, plan_chunks, plan_audio_chunks
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
        self.assertEqual(len(plan_chunks(1426.09, auto_chunk_seconds(), 3)), 1)

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
            plan_chunks(100, 20, 2)

    def test_clock(self):
        self.assertEqual(clock(3661), "01:01:01")


class CredentialsTests(unittest.TestCase):
    def test_explicit_env_has_priority(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-for-test"}):
            self.assertEqual(get_api_key(), "fake-for-test")


if __name__ == "__main__":
    unittest.main()
