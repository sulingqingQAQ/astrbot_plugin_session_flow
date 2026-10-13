# -*- coding: utf-8 -*-
"""v0.1.5：把包装扩到别的钩子——请求里的历史 / 工具集 / 图片 / 模型被谁改了、回复被谁改写、
实际发出的内容和模型原话不一样时补一条“实际发出”、所有钩子上的处理函数耗时。

仿宿主：每种钩子一份登记表（handler 是 functools.partial(原函数, 插件实例)），逐个 await 调用。
"""

from __future__ import annotations

import asyncio
import functools
import json
import unittest
import weakref
from types import SimpleNamespace

import fakes
from sf import attrib as attrib_mod
from sf import extract as ex
from sf import search as search_mod


# ---- 仿 AstrBot 消息组件：类名就是组件名（签名按类名认）
class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    pass


class At:
    def __init__(self, qq):
        self.qq = qq


class Reply:
    pass


class ChainResult:
    """仿 MessageEventResult：有 chain / result_content_type / is_llm_result。"""

    def __init__(self, comps, ctype="LLM_RESULT"):
        self.chain = list(comps)
        self.result_content_type = fakes._CType(ctype)

    def is_llm_result(self):
        return self.result_content_type.name == "LLM_RESULT"


class Req(fakes.FakeReq):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.extra_user_content_parts = []


CTX = [{"role": "user", "content": f"历史问题{i}"} if i % 2 == 0 else {"role": "assistant", "content": f"历史回答{i}"}
       for i in range(6)]


# ---- 假插件
class Hist:
    async def trim(self, event, req):
        req.contexts = req.contexts[-2:]

    async def rebuild_copies(self, event, req):
        req.contexts = [dict(m) for m in req.contexts]      # 全换成拷贝，内容没变

    async def append_sys(self, event, req):
        req.contexts = list(req.contexts) + [{"role": "system", "content": "<mem>记忆</mem>"}]


class Tools:
    async def drop(self, event, req):
        req.func_tool = fakes.FakeFuncTool(["keep"])

    async def add(self, event, req):
        req.func_tool = fakes.FakeFuncTool(req.func_tool.names() + ["extra"])


class Misc:
    async def img_model(self, event, req):
        req.image_urls = ["http://x/1.png"]
        req.model = "other-model"

    async def noop(self, event, req):
        return None


class Resp:
    async def clean(self, event, resp):
        resp.completion_text = resp.completion_text.replace("（", "")

    async def boom(self, event, resp):
        resp.completion_text += "半截"
        raise RuntimeError("回复钩子炸了")

    async def noop(self, event, resp):
        return None


class Deco:
    async def suffix(self, event):
        event.get_result().chain.append(Plain(" ~喵"))

    async def add_image(self, event):
        event.get_result().chain.append(Image())

    async def at_and_trim(self, event):          # 只加 @ 和去尾空白：不算内容变了
        res = event.get_result()
        res.chain.insert(0, At("10001"))
        res.chain[-1].text = res.chain[-1].text.rstrip()

    async def clear(self, event):
        event.get_result().chain.clear()

    async def noop(self, event):
        return None


class Slow:
    async def done(self, event, run_context, response):
        return None

    async def sent(self, event):
        return None


def meta(cls, inst, fn, module, prio=0):
    return SimpleNamespace(handler=functools.partial(getattr(cls, fn), inst), handler_name=fn,
                           handler_module_path=module, extras_configs={"priority": prio})


class Host:
    """每种钩子一份登记表；call() 逐个 await，异常吞掉（仿 call_event_hook）。"""

    def __init__(self, tables):
        self.tables = tables
        self.star_map = {
            "plugins.hist.main": SimpleNamespace(name="plugin_hist"),
            "plugins.tools.main": SimpleNamespace(name="plugin_tools"),
            "plugins.misc.main": SimpleNamespace(name="plugin_misc"),
            "plugins.resp.main": SimpleNamespace(name="plugin_resp"),
            "plugins.deco.main": SimpleNamespace(name="plugin_deco"),
            "plugins.slow.main": SimpleNamespace(name="plugin_slow"),
            "sf_plugin.main": SimpleNamespace(name="astrbot_plugin_session_flow"),
        }

    def __call__(self):
        return {k: list(v) for k, v in self.tables.items()}, self.star_map

    async def call(self, hook, *args, swallow=True):
        for m in self.tables.get(hook, []):
            try:
                await m.handler(*args)
            except BaseException:
                if not swallow:
                    raise


def run(coro):
    return asyncio.run(coro)


def make(tables):
    rec, st, nm, _ = fakes.make_recorder()
    host = Host(tables)
    att = attrib_mod.Attributor(rec, own_package="sf_plugin", host=host)
    return rec, st, att, host


def drive(rec, att, host, *, req=None, reply="好（的）", span="sp-x", result_comps=None, before_sent=None,
          call_sent=True):
    """请求 → 回复 → agent_done → 发送前。返回 (事件, 请求, 回复对象)。"""
    ev = fakes.FakeEvent(umo=fakes.UMO_WC, span_id=span)
    req = req or Req(prompt="掷个骰子", system_prompt="你是助手", contexts=list(CTX), tools=["t1", "t2", "keep"])
    rec.on_waiting(ev)
    rec.on_request_first(ev, req)
    att.ensure_wrapped()
    run(host.call("on_llm_request", ev, req))
    rec.on_request_last(ev, req)
    ctx = fakes.FakeCtx([{"role": "system", "content": req.system_prompt}, fakes.user_msg(req.prompt)])
    rec.on_agent_begin(ev, ctx)
    ctx.messages.append(fakes.assistant_msg(reply))             # 消息里是模型原话
    resp = fakes.FakeResp(reply)
    run(host.call("on_llm_response", ev, resp))                  # 回复钩子可能改写
    rec.on_llm_response(ev, resp)
    rec.on_agent_done(ev, ctx, resp)
    ev.set_result(ChainResult(result_comps if result_comps is not None else [Plain(resp.completion_text)]))
    if before_sent:
        before_sent(ev)
    run(host.call("on_decorating_result", ev))                   # 发送前钩子
    if call_sent:
        rec.on_sent(ev)
    return ev, req, resp


def events(st, run_id, kind):
    out = []
    for e in st.events_for(run_id):
        if e["kind"] == kind:
            out.append({"name": e["name"], "text": e["text"], "data": json.loads(e["data"]) if e["data"] else {}})
    return out


class TestRequestEdits(unittest.TestCase):
    def test_history_trim_names_its_plugin(self):
        rec, st, att, host = make({"on_llm_request": [meta(Hist, Hist(), "trim", "plugins.hist.main", 3)]})
        drive(rec, att, host)
        got = events(st, "sp-x#1", "edit")
        self.assertEqual([g["name"] for g in got], ["历史"])
        self.assertEqual(got[0]["data"]["plugin"], "plugin_hist")
        self.assertEqual(got[0]["data"]["handler"], "trim")
        self.assertEqual(got[0]["data"]["attrib"], "exact")
        self.assertEqual(got[0]["data"]["priority"], 3)
        self.assertIn("历史 6 → 2 条", got[0]["text"])
        self.assertIn("删去 4 条", got[0]["text"])
        st.close()

    def test_replacing_with_copies_is_not_a_change(self):
        rec, st, att, host = make({"on_llm_request": [meta(Hist, Hist(), "rebuild_copies", "plugins.hist.main")]})
        drive(rec, att, host)
        self.assertEqual(events(st, "sp-x#1", "edit"), [])
        st.close()

    def test_history_append_shows_added_message(self):
        rec, st, att, host = make({"on_llm_request": [meta(Hist, Hist(), "append_sys", "plugins.hist.main")]})
        drive(rec, att, host)
        got = events(st, "sp-x#1", "edit")
        self.assertIn("新增或改写 1 条", got[0]["text"])
        self.assertIn("<mem>记忆</mem>", got[0]["text"])
        st.close()

    def test_toolset_drop_and_add(self):
        rec, st, att, host = make({"on_llm_request": [meta(Tools, Tools(), "drop", "plugins.tools.main"),
                                                      meta(Tools, Tools(), "add", "plugins.tools.main", -1)]})
        drive(rec, att, host)
        got = {g["data"]["handler"]: g for g in events(st, "sp-x#1", "edit")}
        self.assertIn("去掉：t1、t2", got["drop"]["text"])
        self.assertIn("工具集 3 → 1 个", got["drop"]["text"])
        self.assertIn("新增：extra", got["add"]["text"])
        st.close()

    def test_images_and_model(self):
        rec, st, att, host = make({"on_llm_request": [meta(Misc, Misc(), "img_model", "plugins.misc.main")]})
        drive(rec, att, host)
        got = {g["name"]: g for g in events(st, "sp-x#1", "edit")}
        self.assertIn("图片 0 → 1 张", got["图片"]["text"])
        self.assertIn("test-model → other-model", got["模型"]["text"])
        st.close()

    def test_untouched_handler_leaves_nothing_but_is_timed_with_its_hook(self):
        rec, st, att, host = make({"on_llm_request": [meta(Misc, Misc(), "noop", "plugins.misc.main")]})
        drive(rec, att, host)
        self.assertEqual(events(st, "sp-x#1", "edit"), [])
        h = att.stats()["handlers"]["plugin_misc.noop"]
        self.assertEqual((h["hook"], h["count"]), ("on_llm_request", 1))
        st.close()

    def test_edits_do_not_break_injection_attribution(self):
        class Both:
            async def go(self, event, req):
                req.system_prompt += "\n\n<lore>设定</lore>"
                req.contexts = req.contexts[-1:]
        rec, st, att, host = make({"on_llm_request": [meta(Both, Both(), "go", "plugins.hist.main")]})
        drive(rec, att, host)
        inj = events(st, "sp-x#1", "inject")
        self.assertEqual(inj[0]["name"], "lore")
        self.assertEqual(inj[0]["data"]["plugin"], "plugin_hist")
        self.assertEqual([g["name"] for g in events(st, "sp-x#1", "edit")], ["历史"])
        st.close()


class TestRewriteAndSent(unittest.TestCase):
    def test_same_reply_means_no_sent_event(self):
        rec, st, att, host = make({"on_llm_response": [meta(Resp, Resp(), "noop", "plugins.resp.main")],
                                   "on_decorating_result": [meta(Deco, Deco(), "noop", "plugins.deco.main")]})
        drive(rec, att, host)
        self.assertEqual(events(st, "sp-x#1", "sent"), [])
        st.close()

    def test_response_rewrite_is_attributed_and_original_reply_is_kept(self):
        rec, st, att, host = make({"on_llm_response": [meta(Resp, Resp(), "clean", "plugins.resp.main", 2)]})
        drive(rec, att, host)
        sent = events(st, "sp-x#1", "sent")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["name"], "实际发出")
        self.assertEqual(sent[0]["text"], "好的）")
        d = sent[0]["data"]
        self.assertEqual(d["attrib"], "exact")
        w = d["rewrites"][0]
        self.assertEqual((w["hook"], w["plugin"], w["handler"], w["priority"]), ("on_llm_response", "plugin_resp", "clean", 2))
        self.assertEqual((w["before"], w["after"]), ("好（的）", "好的）"))
        reply = [e for e in st.events_for("sp-x#1") if e["kind"] == "text"]
        self.assertEqual(reply[0]["text"], "好（的）")              # 模型原话那条还在
        row = st.get_run_row("sp-x#1")
        self.assertEqual(row["reply_text"], "好（的）")
        st.close()

    def test_decorating_rewrite_text_and_image(self):
        rec, st, att, host = make({"on_decorating_result": [meta(Deco, Deco(), "suffix", "plugins.deco.main"),
                                                            meta(Deco, Deco(), "add_image", "plugins.deco.main", -1)]})
        drive(rec, att, host)
        sent = events(st, "sp-x#1", "sent")
        self.assertEqual(sent[0]["text"], "好（的） ~喵[图片]")
        by = {w["handler"]: w for w in sent[0]["data"]["rewrites"]}
        self.assertEqual(by["suffix"]["hook"], "on_decorating_result")
        self.assertEqual(by["suffix"]["after"], "好（的） ~喵")
        self.assertEqual(by["add_image"]["after"], "好（的） ~喵[图片]")
        st.close()

    def test_both_phases_are_listed_in_order(self):
        rec, st, att, host = make({"on_llm_response": [meta(Resp, Resp(), "clean", "plugins.resp.main")],
                                   "on_decorating_result": [meta(Deco, Deco(), "suffix", "plugins.deco.main")]})
        drive(rec, att, host)
        d = events(st, "sp-x#1", "sent")[0]["data"]
        self.assertEqual([w["hook"] for w in d["rewrites"]], ["on_llm_response", "on_decorating_result"])
        st.close()

    def test_at_and_whitespace_do_not_count(self):
        rec, st, att, host = make({"on_decorating_result": [meta(Deco, Deco(), "at_and_trim", "plugins.deco.main")]})
        drive(rec, att, host, reply="好的\n")
        self.assertEqual(events(st, "sp-x#1", "sent"), [])
        st.close()

    def test_unwrapped_difference_is_unattributed(self):
        rec, st, att, host = make({})
        drive(rec, att, host, before_sent=lambda ev: ev.get_result().chain.append(Plain("（宿主自己加的）")))
        d = events(st, "sp-x#1", "sent")[0]["data"]
        self.assertEqual((d["attrib"], d["rewrites"]), ("unattributed", []))
        st.close()

    def test_cleared_result(self):
        rec, st, att, host = make({"on_decorating_result": [meta(Deco, Deco(), "clear", "plugins.deco.main")]})
        drive(rec, att, host)
        sent = events(st, "sp-x#1", "sent")[0]
        self.assertIn("没发出", sent["text"])
        self.assertTrue(sent["data"]["rewrites"][0]["cleared"])
        st.close()

    def test_result_is_none(self):
        rec, st, att, host = make({})
        ev, req, resp = drive(rec, att, host, call_sent=False)
        ev.set_result(None)
        rec.on_sent(ev)
        self.assertIn("没发出", events(st, "sp-x#1", "sent")[0]["text"])
        st.close()

    def test_streaming_frame_waits_for_finish(self):
        rec, st, att, host = make({})
        ev, req, resp = drive(rec, att, host, call_sent=False)
        ev.set_result(ChainResult([Plain("半")], ctype="STREAMING_RESULT"))
        rec.on_sent(ev)                                              # 中间帧：不处理也不丢
        self.assertEqual(events(st, "sp-x#1", "sent"), [])
        self.assertIn(id(ev), rec.closed_by_eid)
        ev.set_result(ChainResult([Plain("改过的完整回复")], ctype="STREAMING_FINISH"))
        rec.on_sent(ev)
        self.assertEqual(events(st, "sp-x#1", "sent")[0]["text"], "改过的完整回复")
        st.close()

    def test_consumed_once(self):
        rec, st, att, host = make({})
        ev, req, resp = drive(rec, att, host, before_sent=lambda e: e.get_result().chain.append(Plain("多")))
        rec.on_sent(ev)                                              # 第二次什么都不做
        self.assertEqual(len(events(st, "sp-x#1", "sent")), 1)
        self.assertNotIn(id(ev), rec.closed_by_eid)
        st.close()

    def test_recycled_event_id_does_not_get_someone_elses_run(self):
        rec, st, att, host = make({})
        old = fakes.FakeEvent(umo=fakes.UMO_WC, span_id="old")
        new = fakes.FakeEvent(umo=fakes.UMO_WC, span_id="new")
        rec.closed_by_eid[id(new)] = {"run_id": "old#1", "umo": fakes.UMO_WC, "reply": "原话", "rewrites": [],
                                      "t": rec.clock(), "ref": weakref.ref(old)}   # 弱引用指向另一个对象
        new.set_result(ChainResult([Plain("别的内容")]))
        rec.on_sent(new)
        self.assertNotIn(id(new), rec.closed_by_eid)
        self.assertEqual(st.events_for("old#1"), [])
        st.close()

    def test_exception_in_response_hook_is_attributed_and_propagates(self):
        rec, st, att, host = make({"on_llm_response": [meta(Resp, Resp(), "boom", "plugins.resp.main")]})
        ev = fakes.FakeEvent(umo=fakes.UMO_WC, span_id="sp-boom")
        req = Req()
        rec.on_waiting(ev)
        rec.on_request_first(ev, req)
        att.ensure_wrapped()
        rec.on_request_last(ev, req)
        resp = fakes.FakeResp("好")
        with self.assertRaises(RuntimeError):                        # 异常照样抛给宿主，不被我们吞
            run(host.tables["on_llm_response"][0].handler(ev, resp))
        st_run = rec.open_by_eid[id(ev)]
        self.assertEqual(st_run.rewrites[-1]["handler"], "boom")   # 炸之前改的也记下了
        st.close()

    def test_no_note_when_event_not_recorded(self):
        rec, st, att, host = make({"on_llm_response": [meta(Resp, Resp(), "clean", "plugins.resp.main")]})
        ev = fakes.FakeEvent(umo=fakes.UMO_WC, span_id="sp-nr")
        att.ensure_wrapped()
        run(host.call("on_llm_response", ev, fakes.FakeResp("（x）")))   # 没开始记录的事件
        self.assertEqual(rec.open_by_eid, {})
        self.assertEqual(rec.closed_by_eid, {})
        st.close()


class TestAllHooksTimingAndLifecycle(unittest.TestCase):
    def tables(self):
        return {"on_llm_request": [meta(Misc, Misc(), "noop", "plugins.misc.main")],
                "on_llm_response": [meta(Resp, Resp(), "noop", "plugins.resp.main")],
                "on_decorating_result": [meta(Deco, Deco(), "noop", "plugins.deco.main")],
                "on_agent_done": [meta(Slow, Slow(), "done", "plugins.slow.main")],
                "on_after_message_sent": [meta(Slow, Slow(), "sent", "plugins.slow.main")]}

    def test_every_hook_is_timed_and_counted(self):
        rec, st, att, host = make(self.tables())
        att.ensure_wrapped()
        s = att.stats()
        self.assertEqual(s["status"]["wrapped"], 5)
        self.assertEqual(s["status"]["by_hook"], {"on_llm_request": 1, "on_llm_response": 1,
                                                  "on_decorating_result": 1, "on_agent_done": 1,
                                                  "on_after_message_sent": 1})
        ev = fakes.FakeEvent()
        run(host.call("on_agent_done", ev, object(), object()))
        run(host.call("on_after_message_sent", ev))
        h = att.stats()["handlers"]
        self.assertEqual(h["plugin_slow.done"]["hook"], "on_agent_done")
        self.assertEqual(h["plugin_slow.sent"]["hook"], "on_after_message_sent")
        self.assertEqual(h["plugin_slow.done"]["count"], 1)
        self.assertEqual(rec.handler_notes, {})                      # 只统计，不产生笔记
        st.close()

    def test_wrapping_is_idempotent_across_hooks(self):
        rec, st, att, host = make(self.tables())
        att.ensure_wrapped()
        self.assertEqual(att.ensure_wrapped(), 0)
        for lst in host.tables.values():
            for m in lst:
                self.assertTrue(getattr(m.handler.func, "_sf_wrapped", False))
                self.assertFalse(getattr(m.handler.func._sf_orig, "_sf_wrapped", False))   # 没叠两层
        st.close()

    def test_restore_unwraps_all_hooks(self):
        rec, st, att, host = make(self.tables())
        att.ensure_wrapped()
        self.assertEqual(att.restore(), 5)
        for lst in host.tables.values():
            for m in lst:
                self.assertFalse(getattr(m.handler.func, "_sf_wrapped", False))
        self.assertEqual(att.stats()["status"]["by_hook"], {})
        st.close()

    def test_reload_rebinds_new_instance_for_every_hook(self):
        rec, st, att, host = make(self.tables())
        att.ensure_wrapped()
        for lst in host.tables.values():       # 仿插件管理器重载：raw = handler.func；handler = partial(raw, 新实例)
            for m in lst:
                raw = m.handler.func
                m.handler = functools.partial(raw, object())
        att.ensure_wrapped()
        for lst in host.tables.values():
            for m in lst:
                self.assertTrue(getattr(m.handler.func, "_sf_wrapped", False))
                self.assertFalse(getattr(m.handler.func._sf_orig, "_sf_wrapped", False))
        st.close()

    def test_own_hooks_are_skipped(self):
        own = SimpleNamespace(handler=functools.partial(Resp.noop, Resp()), handler_name="mine",
                              handler_module_path="sf_plugin.main", extras_configs={})
        rec, st, att, host = make({"on_llm_response": [own]})
        self.assertEqual(att.ensure_wrapped(), 0)
        st.close()

    def test_old_style_host_still_works(self):
        # 旧测试 / 旧接口：host() 只给 on_llm_request 的列表
        rec, st, nm, _ = None, None, None, None
        rec, st, nm, _ = fakes.make_recorder()
        m = meta(Misc, Misc(), "noop", "plugins.misc.main")
        att = attrib_mod.Attributor(rec, host=lambda: ([m], {"plugins.misc.main": SimpleNamespace(name="plugin_misc")}))
        self.assertEqual(att.ensure_wrapped(), 1)
        self.assertEqual(att.stats()["status"]["by_hook"], {"on_llm_request": 1})
        st.close()


class TestHelpersAndSearch(unittest.TestCase):
    def test_chain_helpers(self):
        sig = ex.chain_sig([Plain("你 好"), At("1"), Image(), Reply()])
        self.assertEqual(ex.chain_text(sig), "你 好[@1][图片][引用]")
        self.assertEqual(ex.sent_signature(sig), ("你好", ("Image",)))
        self.assertEqual(ex.result_sig(None), ())
        self.assertEqual(ex.result_sig(fakes.FakeResult("纯文本")), (("Plain", "纯文本"),))

    def test_ctx_fingerprint_handles_part_lists_and_objects(self):
        fp = ex.ctx_fingerprint([{"role": "user", "content": [{"type": "text", "text": "你好"}]},
                                 {"role": "assistant", "content": "x" * 300}], snip=10)
        self.assertEqual(fp[0], ("user", "你好", 2))
        self.assertEqual(fp[1], ("assistant", "x" * 10, 300))

    def test_text_kind_filter_includes_sent_and_edit_kind_exists(self):
        self.assertEqual(search_mod.KIND_GROUPS["text"], ("text", "sent"))
        self.assertEqual(search_mod.KIND_GROUPS["edit"], ("edit",))
        self.assertEqual(search_mod.clean_kind("edit"), "edit")

    def test_search_finds_sent_text_and_edit_runs(self):
        rec, st, att, host = make({"on_llm_response": [meta(Resp, Resp(), "clean", "plugins.resp.main")],
                                   "on_llm_request": [meta(Hist, Hist(), "trim", "plugins.hist.main")]})
        drive(rec, att, host, reply="龙虾（沙拉）")
        res = search_mod.search_runs_page(st, "龙虾沙拉）", umo=fakes.UMO_WC, kind="text")
        self.assertEqual(res["ids"], ["sp-x#1"])                      # 只有改写后的“实际发出”里有这串字
        self.assertEqual(res["hits"]["sp-x#1"][0]["kind"], "sent")
        res = search_mod.search_runs_page(st, "", umo=fakes.UMO_WC, kind="edit")
        self.assertEqual(res["ids"], ["sp-x#1"])
        st.close()


if __name__ == "__main__":
    unittest.main()
