import unittest

from scribeflow.formatting import format_paragraphs


class FormattingTests(unittest.TestCase):
    def test_conversation_is_readable_without_changing_content(self):
        text = (
            "안녕하세요? 네, 안녕하세요. "
            "이전 프로젝트에서 백엔드 개발을 맡았습니다. 그때 가장 어려웠던 점은 연결 문제였습니다. "
            "그래서 해결하려고 기록했어요. 잘됐나요? 네. "
        ) * 5
        formatted = format_paragraphs(text)
        self.assertIn("\n\n", formatted)
        self.assertEqual("".join(formatted.split()), "".join(text.split()))
        self.assertNotIn("[화자", formatted)
        self.assertNotIn("요약", formatted)

    def test_long_punctuationless_speech_is_wrapped(self):
        text = " ".join(["그렇게했습니다"] * 120)
        self.assertIn("\n\n", format_paragraphs(text))
        self.assertEqual("".join(format_paragraphs(text).split()), "".join(text.split()))

    def test_empty_and_short(self):
        self.assertEqual(format_paragraphs("  \n"), "")
        self.assertEqual(format_paragraphs("네. 맞아요."), "네. 맞아요.\n")


if __name__ == "__main__":
    unittest.main()
