# -*- coding: utf-8 -*-
"""注入识别：别的插件往 system prompt / 提问里塞的东西要单独列成「注入」。"""

from __future__ import annotations

import json
import unittest

import fakes
from sf import extract as ex


class TestDiffInjections(unittest.TestCase):
    def test_unchanged_is_empty(self):
        self.assertEqual(ex.diff_injections("你是助手", "你是助手", "系统提示词", "system_prompt"), [])
        self.assertEqual(ex.diff_injections(None, "x", "系统提示词", "system_prompt"), [])  # 没拿到快照不乱猜

    def test_suffix_tags_split_by_name(self):
        base = "你是助手"
        final = base + "\n\n<lore>设定A</lore>\n\n<memory>记忆B</memory>"
        got = ex.diff_injections(base, final, "系统提示词", "system_prompt")
        self.assertEqual([g["name"] for g in got], ["lore", "memory"])
        self.assertEqual(got[0]["text"], "<lore>设定A</lore>")
        self.assertTrue(all(g["source"] == "system_prompt" for g in got))

    def test_plain_suffix_and_prefix(self):
        got = ex.diff_injections("你是助手", "前面的话\n你是助手\n后面的话", "系统提示词", "system_prompt")
        self.assertEqual([(g["name"], g["text"]) for g in got],
                         [("系统提示词前置", "前面的话"), ("系统提示词追加", "后面的话")])

    def test_base_empty_all_is_added(self):
        got = ex.diff_injections("", "整段都是插件加的", "系统提示词", "system_prompt")
        self.assertEqual([g["name"] for g in got], ["系统提示词追加"])

    def test_rewrite_in_middle(self):
        got = ex.diff_injections("今天是{date}，你是助手", "今天是2026-10-12，你是助手", "系统提示词", "system_prompt")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "系统提示词改写")
        self.assertEqual(got[0]["text"], "2026-10-12")
        self.assertEqual(got[0]["removed"], "{date}")


class TestRecorderCapturesPromptInjections(unittest.TestCase):
    def _run(self, mutate, umo=fakes.UMO_WC, cfg=None):
        rec, st, nm, _ = fakes.make_recorder(cfg)
        ev = fakes.FakeEvent(umo=umo, span_id="sp-inj")
        req = fakes.FakeReq(prompt="掷个骰子", system_prompt="你是助手")
        rec.on_waiting(ev)
        rec.on_request_first(ev, req)         # 别的插件改之前
        mutate(req)                            # 别的插件动手
        rec.on_request_last(ev, req)
        ctx = fakes.FakeCtx([{"role": "system", "content": req.system_prompt}, fakes.user_msg(req.prompt)])
        rec.on_agent_begin(ev, ctx)
        ctx.messages.append(fakes.assistant_msg("好"))
        rec.on_llm_response(ev, fakes.FakeResp("好"))
        rec.on_agent_done(ev, ctx, fakes.FakeResp("好"))
        return rec, st, [e for e in st.events_for("sp-inj#1") if e["kind"] == "inject"]

    def test_system_prompt_injection_listed(self):
        def mutate(req):
            req.system_prompt += "\n\n<lore>设定A</lore>"
        rec, st, inj = self._run(mutate)
        self.assertEqual([e["name"] for e in inj], ["lore"])
        self.assertEqual(json.loads(inj[0]["data"])["source"], "system_prompt")
        self.assertIn("设定A", inj[0]["text"])
        self.assertEqual(rec.first_req, {})   # 用完就清，不留着占内存
        st.close()

    def test_prompt_prefix_injection(self):
        def mutate(req):
            req.prompt = "[群聊·小明] " + req.prompt
        rec, st, inj = self._run(mutate)
        self.assertEqual([e["name"] for e in inj], ["提问前置"])
        self.assertEqual(json.loads(inj[0]["data"])["source"], "prompt")
        st.close()

    def test_nothing_changed_no_inject(self):
        rec, st, inj = self._run(lambda req: None)
        self.assertEqual(inj, [])
        st.close()

    def test_tag_in_prompt_not_duplicated(self):
        # 提问里追加的标签块，agent_begin 的消息里还会再出现；同名同文只留一条（带来源的）
        def mutate(req):
            req.prompt += "<system_reminder>现在是晚上</system_reminder>"
        rec, st, inj = self._run(mutate)
        names = [e["name"] for e in inj]
        self.assertEqual(names.count("system_reminder"), 1, names)
        st.close()

    def test_excluded_umo_keeps_no_snapshot(self):
        rec, st, nm, _ = fakes.make_recorder({"exclude_umos": [fakes.UMO_QQ]})
        ev = fakes.FakeEvent(umo=fakes.UMO_QQ, span_id="sp-x")
        rec.on_request_first(ev, fakes.FakeReq())
        self.assertEqual(rec.first_req, {})
        st.close()

    def test_old_signature_still_works(self):
        rec, st, nm, _ = fakes.make_recorder()
        rec.on_request_first(fakes.FakeEvent(span_id="sp-y"))   # 没传 req 的老调用
        self.assertEqual(rec.first_req, {})
        st.close()

    def test_sweep_drops_stale_snapshots(self):
        rec, st, nm, _ = fakes.make_recorder()
        ev = fakes.FakeEvent(span_id="sp-z")
        rec.on_request_first(ev, fakes.FakeReq())
        self.assertEqual(len(rec.first_req), 1)
        now = rec.clock()
        rec.clock = lambda: now + 3600
        rec._sweep_temp()
        self.assertEqual(rec.first_req, {})
        st.close()


if __name__ == "__main__":
    unittest.main()
