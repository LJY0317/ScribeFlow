"""Offline accuracy tests; no audio, network, or API keys required."""

import itertools
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scribeflow.cli import main
from scribeflow.quality import comparable_text, compare_transcripts, edit_distance


def dynamic_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(
                previous[j] + 1, current[-1] + 1,
                previous[j - 1] + (char_a != char_b),
            ))
        previous = current
    return previous[-1]


class QualityTests(unittest.TestCase):
    def test_bit_parallel_distance_matches_dynamic_programming(self):
        samples = [""]
        for n in range(1, 5):
            samples.extend("".join(chars) for chars in itertools.product("가a", repeat=n))
        for a in samples:
            for b in samples:
                with self.subTest(a=a, b=b):
                    self.assertEqual(edit_distance(a, b), dynamic_distance(a, b))

    def test_normalization_ignores_only_presentation(self):
        self.assertEqual(comparable_text("안녕, 세상!"), "안녕세상")
        self.assertEqual(comparable_text(" \tHELLO\n"), "hello")
        self.assertEqual(comparable_text("가"), comparable_text("가"))
        self.assertEqual(
            comparable_text(
                "안녕하세요.\n\n<!-- ScribeFlow SHA256: " + "a" * 64 + " -->\n"
            ),
            "안녕하세요",
        )

    def test_comparison_and_keyword_diagnostics(self):
        result = compare_transcripts(
            "하나손해보험에서 대인접수와 구상권을 설명했습니다",
            "하나손해보험에서 대물접수와 구상건을 설명했습니다",
            "하나손해보험에서 대인접수와 구상권을 설명했습니다",
            keywords=["하나손해보험", "대인접수", "구상권"],
        )
        self.assertGreater(result["baseline"]["cer"], 0)
        self.assertEqual(result["candidate"]["cer"], 0)
        self.assertGreater(result["cer_reduction"], 0)
        self.assertEqual(result["keyword_occurrences_unaligned"][1]["baseline_count"], 0)
        self.assertEqual(result["keyword_occurrences_unaligned"][1]["candidate_count"], 1)

    def test_reference_is_mandatory(self):
        with self.assertRaisesRegex(ValueError, "기준 전사문"):
            compare_transcripts(" \n!!", "안녕하세요", "안녕하세요")

    def test_long_korean_transcripts_do_not_need_quadratic_memory(self):
        reference = "여기는장시간한국어전사정확도검사입니다" * 300
        candidate = reference[:-1] + "요"
        self.assertEqual(edit_distance(reference, candidate), 1)

    def test_evaluate_cli_is_read_only_and_never_calls_transcription(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ref, old, new = [
                root / filename for filename in ("human.txt", "old.txt", "new.txt")
            ]
            ref.write_text("대인 접수\n", encoding="utf-8")
            old.write_text("대물 접수\n", encoding="utf-8")
            new.write_text("대인 접수\n", encoding="utf-8")
            before = {p.name: p.read_bytes() for p in (ref, old, new)}
            stdout = StringIO()
            with patch("scribeflow.cli.transcribe") as transcribe:
                with redirect_stdout(stdout):
                    status = main([
                        "evaluate", str(ref), str(old), str(new),
                        "--keyword", "대인접수",
                    ])
                transcribe.assert_not_called()
            self.assertEqual(status, 0)
            data = json.loads(stdout.getvalue())
            self.assertEqual(data["candidate"]["cer"], 0)
            self.assertGreater(data["baseline"]["cer"], 0)
            self.assertEqual(
                before,
                {p.name: p.read_bytes() for p in (ref, old, new)},
            )
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["human.txt", "new.txt", "old.txt"],
            )


if __name__ == "__main__":
    unittest.main()
