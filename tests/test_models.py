import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "momentum-dashboard"))

import models  # noqa: E402
from models import ModelError, chat_json, extract_json  # noqa: E402


class ExtractJsonTests(unittest.TestCase):
    def test_fence_and_surrounding_text(self):
        text = "以下是结果：\n```json\n{\"a\": 1, \"b\": [1, 2]}\n```\n结束"
        self.assertEqual({"a": 1, "b": [1, 2]}, extract_json(text))

    def test_trailing_comma_repair(self):
        text = '{"a": 1, "b": [1, 2,],}'
        self.assertEqual({"a": 1, "b": [1, 2]}, extract_json(text))

    def test_no_json_raises(self):
        with self.assertRaises(ModelError):
            extract_json("没有任何 JSON 对象")

    def test_broken_json_raises(self):
        with self.assertRaises(ModelError):
            extract_json('{"a": [1, 2,}')


class ChatJsonRetryTests(unittest.TestCase):
    def test_retry_once_with_repair_hint(self):
        calls = []

        def fake_chat(provider, system, user_text, images=None, max_tokens=None):
            calls.append(user_text)
            if len(calls) == 1:
                return '{"a": 1, "b": [1, 2'  # 截断且不可修复
            return '{"ok": true}'

        original = models.chat
        models.chat = fake_chat
        try:
            parsed, _ = chat_json("p", "sys", "user", retries=1)
        finally:
            models.chat = original
        self.assertEqual({"ok": True}, parsed)
        self.assertEqual(2, len(calls))
        self.assertIn("JSON 不合法", calls[1])

    def test_retries_exhausted_raises(self):
        def fake_chat(provider, system, user_text, images=None, max_tokens=None):
            return "not json at all"

        original = models.chat
        models.chat = fake_chat
        try:
            with self.assertRaises(ModelError):
                chat_json("p", "sys", "user", retries=1)
        finally:
            models.chat = original


if __name__ == "__main__":
    unittest.main()
