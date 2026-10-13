# -*- coding: utf-8 -*-
"""extract 测试：拆分 / 提取 / 剥图 / 截断。"""

from __future__ import annotations

import unittest

from sf import extract as ex


class TestSplitUser(unittest.TestCase):
    def test_list_first_text_is_spoken(self):
        spoken, injects, images = ex.split_user_content([
            {"type": "text", "text": "掷个骰子"},
            {"type": "text", "text": "<system_reminder>现在时间</system_reminder>"},
            {"type": "text", "text": "<RAG>记忆</RAG>"},
        ])
        self.assertEqual(spoken, "掷个骰子")
        self.assertEqual([i["name"] for i in injects], ["system_reminder", "RAG"])
        self.assertEqual(images, 0)

    def test_list_counts_images(self):
        spoken, injects, images = ex.split_user_content([
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
        ])
        self.assertEqual(spoken, "看这张图")
        self.assertEqual(images, 1)

    def test_string_tag_split(self):
        spoken, injects, _ = ex.split_user_content(
            "你好<system_reminder>Current datetime</system_reminder>尾巴")
        self.assertIn("你好", spoken)
        self.assertIn("尾巴", spoken)
        self.assertEqual(injects[0]["name"], "system_reminder")

    def test_string_no_tag(self):
        spoken, injects, _ = ex.split_user_content("纯文本")
        self.assertEqual(spoken, "纯文本")
        self.assertEqual(injects, [])


class TestAssistant(unittest.TestCase):
    def test_think_text_tool_calls(self):
        segs = ex.assistant_segments([
            {"type": "think", "think": "先调工具"},
            {"type": "text", "text": "掷出来是 8"},
        ])
        self.assertEqual([(s["kind"], s["text"]) for s in segs],
                         [("think", "先调工具"), ("text", "掷出来是 8")])
        tcs = ex.tool_calls_of({
            "role": "assistant", "content": [],
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "dice", "arguments": '{"sides": 20}'}}]})
        self.assertEqual(tcs[0]["name"], "dice")
        self.assertEqual(tcs[0]["id"], "call_1")

    def test_string_content(self):
        self.assertEqual(ex.assistant_segments("嗨"),
                         [{"kind": "text", "text": "嗨"}])
        self.assertEqual(ex.assistant_segments(""), [])


class TestImagesTruncate(unittest.TestCase):
    def test_base64_stripped(self):
        text, n = ex.strip_images_in_text("看图 data:image/png;base64,iVBOR1234567890 结束")
        self.assertEqual(n, 1)
        self.assertIn("base64 图片已省略", text)
        self.assertNotIn("iVBOR", text)

    def test_truncate_marks_origin_len(self):
        text, cut = ex.truncate("abcdef", 3)
        self.assertIn("已截断", text)
        self.assertEqual(cut["truncated"], 6)

    def test_no_truncate(self):
        text, cut = ex.truncate("ab", 10)
        self.assertEqual((text, cut), ("ab", None))

    def test_error_prefix(self):
        self.assertTrue(ex.is_error_result_text("error: 炸了"))
        self.assertFalse(ex.is_error_result_text("正常结果"))

    def test_pretty_args(self):
        self.assertIn("sides", ex.pretty_json_args('{"sides": 20}'))
        self.assertEqual(ex.pretty_json_args(None), "")


if __name__ == "__main__":
    unittest.main()
