# -*- coding: utf-8 -*-
"""v0.1.6：单轮导出（api/export）——正文 / system prompt / 注入全文和归属都在，会话 ID 默认打码。"""

from __future__ import annotations

import asyncio
import json
import unittest

import fakes
from sf import export as export_mod
from sf import store as store_mod
from sf import webapi as webapi_mod

UMO = fakes.UMO_QQ            # default:GroupMessage:100000999
SENDER = "123456789"
SP_TEXT = "你是小苏。\n<memory>不要说出去</memory>\n" + "长" * 3000
BIG = "注入正文" * 7000       # 28000 字：导出里要全文，不能被截


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def make(*, events=None, sysprompt=True, **kw):
    st = store_mod.Store(fakes.tmp_db_path())
    h = "ab" * 32
    if sysprompt:
        st.put_blob(h, SP_TEXT)
    f = {"run_id": "r1#1", "umo": UMO, "cid": "c1", "source": "live", "status": "done",
         "sort_at": 1790000100.0, "ord": 1, "received_at": 1790000100.5, "begin_at": 1790000108.5,
         "end_at": 1790000135.5, "sender_id": SENDER, "sender_name": "小明", "provider_id": "p/deepseek",
         "model": "deepseek-v4.1-flash", "user_text": "你好，在吗", "reply_text": "在的",
         "sysprompt_hash": h if sysprompt else None, "contexts_len": 6, "n_steps": 1, "n_tools": 0,
         "token_in": 190, "token_cached": 11000, "token_out": 88,
         "tools_available": ["dice", "web_search"]}
    f.update(kw)
    st.insert_run(f)
    evs = events if events is not None else [
        {"seq": 1, "t": 1790000100.5, "kind": "user", "text": "你好，在吗"},
        {"seq": 2, "t": 1790000108.4, "kind": "inject", "step": 0, "name": "memory", "text": BIG,
         "data": {"source": "system_prompt", "attrib": "exact", "plugin": "astrbot_plugin_livingmemory",
                  "handler": "handle_memory_recall", "priority": 0}},
        {"seq": 3, "t": 1790000108.4, "kind": "inject", "step": 0, "name": "persona", "text": "你是小苏",
         "data": {"source": "system_prompt", "attrib": "earlier"}},
        {"seq": 4, "t": 1790000108.4, "kind": "edit", "step": 0, "name": "历史", "text": "历史 20 → 2 条",
         "data": {"plugin": "astrbot_plugin_cost_control", "handler": "on_llm_request_head", "attrib": "exact"}},
        {"seq": 5, "t": 1790000135.5, "kind": "text", "text": "在的\n```python\nprint(1)\n```"},
        {"seq": 6, "t": 1790000135.9, "kind": "sent", "step": 0, "name": "实际发出", "text": "在的（改）",
         "data": {"attrib": "exact", "rewrites": [
             {"hook": "on_decorating_result", "plugin": "astrbot_plugin_x", "handler": "dec", "priority": 5,
              "before": "在的", "after": "在的（改）"}]}},
    ]
    st.insert_events([{"run_id": "r1#1", **e} for e in evs])
    api = webapi_mod.SessionFlowWebApi(st, None)
    api.plugin_version = "v9.9.9"
    return st, api


class TestExport(unittest.TestCase):
    def test_route_registered(self):
        names = {r for r, _, _ in webapi_mod.SessionFlowWebApi(None, None).routes()}
        self.assertIn("api/export", names)

    def test_markdown_has_everything(self):
        st, api = make()
        res = run(api.handle_export({"id": "r1#1"}, {}))
        self.assertEqual(res["status"], "ok")
        d = res["data"]
        md = d["text"]
        self.assertTrue(d["filename"].endswith(".md"))
        self.assertRegex(d["filename"], r"^session_flow_run_\d{8}-\d{6}_r11\.md$")   # 时间戳 + run_id 前缀
        self.assertEqual(d["fmt"], "md")
        self.assertIn(SP_TEXT, md)                                 # system prompt 全文
        self.assertIn(BIG, md)                                     # 注入全文，不截
        self.assertIn("由 astrbot_plugin_livingmemory · handle_memory_recall", md)
        self.assertIn("更早就在", md)
        self.assertIn("来自系统提示词", md)
        self.assertIn("astrbot_plugin_cost_control", md)           # 改动归属
        self.assertIn("改写前", md)
        self.assertIn("在的（改）", md)                            # 实际发出的改写后
        self.assertIn("190 / 11000 / 88", md)                      # 未命中缓存 / 命中缓存 / 输出
        self.assertIn("dice", md)
        self.assertIn("v9.9.9", md)
        self.assertEqual(d["injects"], 2)
        self.assertEqual(d["inject_chars"], len(BIG) + len("你是小苏"))
        self.assertEqual(d["system_prompt_chars"], len(SP_TEXT))
        self.assertIn("没有的东西", md)                            # 写明哪些没记
        st.close()

    def test_ids_masked_by_default_and_raw_keeps(self):
        st, api = make()
        md = run(api.handle_export({"id": "r1#1"}, {}))["data"]["text"]
        self.assertNotIn("100000999", md)
        self.assertNotIn(SENDER, md)
        self.assertNotIn("小明", md)
        self.assertIn("default:GroupMessage:#", md)
        raw = run(api.handle_export({"id": "r1#1", "raw": "1"}, {}))["data"]
        self.assertIn("100000999", raw["text"])
        self.assertIn(SENDER, raw["text"])
        self.assertFalse(raw["masked"])
        st.close()

    def test_fence_survives_backticks_in_text(self):
        st, api = make()
        md = run(api.handle_export({"id": "r1#1"}, {}))["data"]["text"]
        # 回复里有 ``` 围栏，外层必须用更长的围栏，不然会提前收尾
        self.assertIn("````text\n在的\n```python\nprint(1)\n```\n````", md)
        st.close()
        self.assertEqual(export_mod._fence("a")[0], "```")
        self.assertEqual(export_mod._fence("x ````` y")[0], "``````")

    def test_json_is_lossless(self):
        st, api = make()
        d = run(api.handle_export({"id": "r1#1", "fmt": "json"}, {}))["data"]
        self.assertTrue(d["filename"].endswith(".json"))
        b = json.loads(d["text"])
        self.assertEqual(b["format"], "session_flow_run/1")
        self.assertTrue(b["masked"])
        self.assertEqual(b["system_prompt"]["text"], SP_TEXT)
        evs = {e["seq"]: e for e in b["events"]}
        self.assertEqual(evs[2]["text"], BIG)
        self.assertEqual(evs[2]["data"]["plugin"], "astrbot_plugin_livingmemory")
        self.assertEqual(evs[6]["data"]["rewrites"][0]["after"], "在的（改）")
        self.assertAlmostEqual(evs[2]["t_rel_s"], 8.4, places=2)    # 距消息时间（sort_at）
        self.assertEqual(b["summary"]["injects"][0]["label"], "astrbot_plugin_livingmemory")   # 按字数排
        self.assertEqual(b["run"]["token_cached"], 11000)
        self.assertNotIn(SENDER, d["text"])
        st.close()

    def test_errors_and_edge_cases(self):
        st, api = make()
        self.assertEqual(run(api.handle_export({}, {}))["status"], "error")
        self.assertEqual(run(api.handle_export({"id": "nope"}, {}))["status"], "error")
        st.close()
        # 没存 system prompt、没有事件的旧档
        st2, api2 = make(events=[], sysprompt=False, source="legacy", status="legacy", run_id="r1#1")
        d = run(api2.handle_export({"id": "r1#1"}, {}))["data"]
        self.assertIn("没存", d["text"])
        self.assertIn("没有事件", d["text"])
        self.assertEqual(d["injects"], 0)
        self.assertEqual(d["system_prompt_chars"], 0)
        st2.close()

    def test_handle_run_unchanged(self):
        # 导出和单轮详情共用 _run_payload，详情的返回形状不能变
        st, api = make()
        r = run(api.handle_run({"id": "r1#1"}, {}))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(set(r["data"]), {"run", "events", "sysprompt", "context", "timing"})   # v0.1.8 起多两块现算的数据
        self.assertEqual(r["data"]["sysprompt"]["size"], len(SP_TEXT.encode("utf-8")))
        st.close()


if __name__ == "__main__":
    unittest.main()
