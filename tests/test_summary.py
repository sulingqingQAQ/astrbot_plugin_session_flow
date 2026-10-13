# -*- coding: utf-8 -*-
"""summary.umo_summary 离线用例。"""

from __future__ import annotations

import unittest

import fakes
from sf import summary as summary_mod

CALL = "call_1"


class TestUmoSummary(unittest.TestCase):
    def test_counts(self):
        rec, st, _, _ = fakes.make_recorder()
        fakes.drive_run(
            rec, span="g1", prompt="掷个骰子",
            tool_uses=[("dice", '{"sides": 6}', "6面")],
            new_messages=[
                fakes.assistant_msg("", tool_calls=[fakes.tool_call_msg(CALL, "dice", '{"sides": 6}')]),
                fakes.tool_msg("6面", CALL),
                fakes.assistant_msg("掷出来是6")],
            llm_resp_text="掷出来是6")
        # 模型失败轮（decorating 阶段关闭）：status=error
        ev = fakes.FakeEvent(span_id="g2")
        rec.on_waiting(ev)
        rec.on_request_last(ev, fakes.FakeReq(prompt="试试坏模型"))
        rec.on_agent_begin(ev, fakes.FakeCtx(
            [{"role": "system", "content": "s"}, fakes.user_msg("试试坏模型")]))
        ev.set_result(fakes.FakeResult("All chat models failed: 炸了", "STREAMING_FINISH", False))
        rec.on_decorating(ev)
        s = summary_mod.umo_summary(st, fakes.UMO_WC)
        self.assertEqual(s["runs"], 2)
        self.assertEqual(s["errors"], 1)
        self.assertEqual(s["legacy_runs"], 0)
        self.assertEqual(len(s["cids"]), 1)
        tops = {t["name"]: t for t in s["tool_top"]}
        self.assertIn("dice", tops)
        self.assertIn("掷个骰子", s["first_task"])
        self.assertIsNotNone(s["last_error"])
        self.assertIn("炸了", s["last_error"]["text"])
        st.close()

    def test_empty_umo(self):
        rec, st, _, _ = fakes.make_recorder()
        s = summary_mod.umo_summary(st, "不存在的umo")
        self.assertEqual(s["runs"], 0)
        self.assertIsNone(s["last_error"])
        st.close()

    def test_tool_error_counts_as_error(self):
        # S3：跑成功但工具报错，卡片/总览/摘要/Top 都要算报错
        rec, st, _, _ = fakes.make_recorder()
        fakes.drive_run(
            rec, span="e1", prompt="试试坏工具",
            tool_uses=[("sf_probe_fail", "{}", None)],
            new_messages=[
                fakes.assistant_msg("", tool_calls=[fakes.tool_call_msg(CALL, "sf_probe_fail", "{}")]),
                fakes.tool_msg("error: 炸了", CALL),
                fakes.assistant_msg("工具报错了")],
            llm_resp_text="工具报错了")
        self.assertEqual(st.list_umos()[0]["error_count"], 1)
        self.assertEqual(st.global_stats()["errors"], 1)
        self.assertEqual(len(st.list_umos(errors_only=True)), 1)
        top = {t["name"]: t for t in st.tool_top()}
        self.assertEqual(top["sf_probe_fail"]["errors"], 1)
        s = summary_mod.umo_summary(st, fakes.UMO_WC)
        self.assertEqual(s["errors"], 1)
        stop = {t["name"]: t for t in s["tool_top"]}
        self.assertEqual(stop["sf_probe_fail"]["errors"], 1)
        st.close()


if __name__ == "__main__":
    unittest.main()
