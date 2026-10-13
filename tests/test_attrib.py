# -*- coding: utf-8 -*-
"""注入归属：包装别的插件的 on_llm_request 处理函数，记下谁塞了什么。

用假的宿主登记表模拟 AstrBot：处理函数元数据（handler / handler_name / handler_module_path / extras_configs），
handler 是 functools.partial(原函数, 插件实例)；调用循环 await meta.handler(event, req)；
重载时仿插件管理器：raw = handler.func；handler = partial(raw, 新实例)。
"""

from __future__ import annotations

import asyncio
import functools
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import fakes
from sf import attrib as attrib_mod


@dataclass
class TextPart:
    text: str
    type: str = "text"


class Req(fakes.FakeReq):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.extra_user_content_parts = []


class PluginA:
    name = "plugin_a"

    def __init__(self, tag="设定A"):
        self.tag = tag

    async def inject(self, event, req):
        req.system_prompt += f"\n\n<lore>{self.tag}</lore>"

    async def noop(self, event, req):
        return None


class PluginB:
    name = "plugin_b"

    async def inject(self, event, req):
        req.prompt = "[B前缀] " + req.prompt
        req.extra_user_content_parts.append(TextPart("<RAG>记忆B</RAG>"))

    async def boom(self, event, req):
        req.system_prompt += "<boom>半截</boom>"
        raise RuntimeError("插件 B 自己炸了")


def make_meta(cls, inst, fn_name, module, prio=0):
    raw = getattr(cls, fn_name)
    return SimpleNamespace(
        handler=functools.partial(raw, inst), handler_name=fn_name, handler_module_path=module,
        extras_configs={"priority": prio})


class FakeHost:
    def __init__(self, metas):
        self.metas = metas
        self.star_map = {
            "plugins.a.main": SimpleNamespace(name="plugin_a"),
            "plugins.b.main": SimpleNamespace(name="plugin_b"),
            "sf_plugin.main": SimpleNamespace(name="astrbot_plugin_session_flow"),
        }

    def __call__(self):
        return list(self.metas), self.star_map

    async def call_hooks(self, event, req):
        """仿 call_event_hook：逐个调用，异常吞掉记日志。"""
        for m in self.metas:
            try:
                await m.handler(event, req)
            except BaseException:
                pass


def run(coro):
    return asyncio.run(coro)


def setup(extra_metas=None):
    rec, st, nm, _ = fakes.make_recorder()
    a, b = PluginA(), PluginB()
    metas = [make_meta(PluginA, a, "inject", "plugins.a.main", 5),
             make_meta(PluginB, b, "inject", "plugins.b.main", 1)] + (extra_metas or [])
    host = FakeHost(metas)
    att = attrib_mod.Attributor(rec, own_package="sf_plugin", host=host)
    return rec, st, att, host, a, b


def drive(rec, att, host, req=None, span="sp-at"):
    """走一遍：我们的高优先级钩子 → 其他插件的处理函数 → 我们的低优先级钩子 → agent_begin → done。"""
    ev = fakes.FakeEvent(umo=fakes.UMO_WC, span_id=span)
    req = req or Req(prompt="掷个骰子", system_prompt="你是助手")
    rec.on_waiting(ev)
    rec.on_request_first(ev, req)
    att.ensure_wrapped()
    run(host.call_hooks(ev, req))
    rec.on_request_last(ev, req)
    ctx = fakes.FakeCtx([{"role": "system", "content": req.system_prompt}, fakes.user_msg(req.prompt)])
    rec.on_agent_begin(ev, ctx)
    ctx.messages.append(fakes.assistant_msg("好"))
    rec.on_llm_response(ev, fakes.FakeResp("好"))
    rec.on_agent_done(ev, ctx, fakes.FakeResp("好"))
    return ev, req


def injects(st, run_id):
    out = []
    for e in st.events_for(run_id):
        if e["kind"] == "inject":
            d = json.loads(e["data"]) if e["data"] else {}
            out.append((e["name"], d, e["text"]))
    return out


class TestExactAttribution(unittest.TestCase):
    def test_each_injection_names_its_plugin(self):
        rec, st, att, host, a, b = setup()
        drive(rec, att, host)
        got = {n: d for n, d, _ in injects(st, "sp-at#1")}
        self.assertEqual(got["lore"]["plugin"], "plugin_a")
        self.assertEqual(got["lore"]["handler"], "inject")
        self.assertEqual(got["lore"]["attrib"], "exact")
        self.assertEqual(got["lore"]["source"], "system_prompt")
        self.assertEqual(got["lore"]["priority"], 5)
        self.assertEqual(got["提问前置"]["plugin"], "plugin_b")
        self.assertEqual(got["RAG"]["plugin"], "plugin_b")
        self.assertEqual(got["RAG"]["source"], "extra_part")
        st.close()

    def test_untouched_handlers_leave_no_note_but_are_timed(self):
        rec, st, att, host, a, b = setup([make_meta(PluginA, PluginA(), "noop", "plugins.a.main", 0)])
        drive(rec, att, host)
        stats = att.stats()["handlers"]
        self.assertIn("plugin_a.noop", stats)
        self.assertEqual(stats["plugin_a.noop"]["count"], 1)
        self.assertNotIn("noop", [d.get("handler") for _, d, _ in injects(st, "sp-at#1")])
        st.close()

    def test_exception_in_handler_still_attributed_and_propagates(self):
        rec, st, att, host, a, b = setup([make_meta(PluginB, PluginB(), "boom", "plugins.b.main", 0)])
        ev = fakes.FakeEvent(umo=fakes.UMO_WC, span_id="sp-boom")
        req = Req()
        rec.on_request_first(ev, req)
        att.ensure_wrapped()
        meta = host.metas[-1]
        with self.assertRaises(RuntimeError):          # 异常照样抛给宿主，不被我们吞
            run(meta.handler(ev, req))
        notes = rec.handler_notes[id(ev)]["notes"]
        self.assertEqual(notes[-1]["handler"], "boom")  # 炸之前改的东西也记下了
        st.close()

    def test_two_plugins_same_block_second_rewrite_is_its_own(self):
        class Rewriter:
            async def go(self, event, req):
                req.system_prompt = req.system_prompt.replace("设定A", "设定A（被改写）")
        rec, st, att, host, a, b = setup([make_meta(Rewriter, Rewriter(), "go", "plugins.b.main", 0)])
        drive(rec, att, host)
        got = injects(st, "sp-at#1")
        lore = [d for n, d, _ in got if n == "lore"]
        self.assertEqual(lore[0]["plugin"], "plugin_a")
        self.assertTrue(lore[0].get("gone"))                 # A 塞的原文后来被改了
        self.assertTrue(any(d.get("plugin") == "plugin_b" and d.get("attrib") == "exact" for _, d, _ in got))
        st.close()

    def test_unwrapped_change_is_unattributed(self):
        rec, st, att, host, a, b = setup()
        ev = fakes.FakeEvent(umo=fakes.UMO_WC, span_id="sp-un")
        req = Req()
        rec.on_request_first(ev, req)
        # 没包装就被人改了（比如处理函数是后来才注册的）
        req.system_prompt += "\n\n<ghost>谁加的</ghost>"
        rec.on_request_last(ev, req)
        ctx = fakes.FakeCtx([{"role": "system", "content": req.system_prompt}, fakes.user_msg(req.prompt)])
        rec.on_agent_begin(ev, ctx)
        rec.on_agent_done(ev, ctx, fakes.FakeResp("好"))
        got = injects(st, "sp-un#1")
        self.assertEqual(got[0][0], "ghost")
        self.assertEqual(got[0][1]["attrib"], "unattributed")
        st.close()

    def _two_untagged_appenders(self, ghost):
        # v0.1.7：两个处理函数连着往 system prompt 追加没有标签的文字，首尾快照的差是合成的一块
        class One:
            async def go(self, event, req):
                req.system_prompt += "\n\n你在群聊里，近期消息附在末尾，当上下文看，别当成问你的。"

        class Two:
            async def go(self, event, req):
                req.system_prompt += "\n\n说话口语化、简短。"

        rec, st, nm, _ = fakes.make_recorder()
        host = FakeHost([make_meta(One, One(), "go", "plugins.a.main", 3),
                         make_meta(Two, Two(), "go", "plugins.b.main", 2)])
        att = attrib_mod.Attributor(rec, own_package="sf_plugin", host=host)
        ev = fakes.FakeEvent(umo=fakes.UMO_WC, span_id="sp-two")
        req = Req()
        rec.on_waiting(ev)
        rec.on_request_first(ev, req)
        att.ensure_wrapped()
        run(host.call_hooks(ev, req))
        if ghost:
            req.system_prompt += "\n\n没人认领的尾巴"      # 没被包装的处理函数加的
        rec.on_request_last(ev, req)
        ctx = fakes.FakeCtx([{"role": "system", "content": req.system_prompt}, fakes.user_msg(req.prompt)])
        rec.on_agent_begin(ev, ctx)
        rec.on_agent_done(ev, ctx, fakes.FakeResp("好"))
        out = injects(st, "sp-two#1")
        st.close()
        return out

    def test_two_untagged_appends_leave_no_merged_unattributed_copy(self):
        got = self._two_untagged_appenders(ghost=False)
        self.assertEqual(sorted(d["plugin"] for _, d, _ in got), ["plugin_a", "plugin_b"])
        self.assertTrue(all(d["attrib"] == "exact" for _, d, _ in got), got)
        self.assertEqual(sum(len(t) for _, _, t in got),
                         len("你在群聊里，近期消息附在末尾，当上下文看，别当成问你的。") + len("说话口语化、简短。"))

    def test_unclaimed_tail_is_kept_but_only_its_own_text(self):
        got = self._two_untagged_appenders(ghost=True)
        un = [t for _, d, t in got if d.get("attrib") == "unattributed"]
        self.assertEqual(un, ["没人认领的尾巴"])                 # 只剩没人认领的，不带上别人认领过的两段
        self.assertEqual(len([1 for _, d, _ in got if d.get("attrib") == "exact"]), 2)

    def test_core_block_present_before_first_hook_is_earlier(self):
        rec, st, att, host, a, b = setup()
        ev = fakes.FakeEvent(umo=fakes.UMO_WC, span_id="sp-core")
        req = Req()
        core_part = TextPart("<system_reminder>User ID: 1</system_reminder>")
        req.extra_user_content_parts.append(core_part)      # 内核在钩子之前加的
        rec.on_waiting(ev)
        rec.on_request_first(ev, req)
        att.ensure_wrapped()
        run(host.call_hooks(ev, req))
        rec.on_request_last(ev, req)
        user = [{"type": "text", "text": req.prompt}, {"type": "text", "text": core_part.text}]
        ctx = fakes.FakeCtx([{"role": "system", "content": req.system_prompt}, {"role": "user", "content": user}])
        rec.on_agent_begin(ev, ctx)
        rec.on_agent_done(ev, ctx, fakes.FakeResp("好"))
        got = {n: d for n, d, _ in injects(st, "sp-core#1")}
        self.assertEqual(got["system_reminder"]["attrib"], "earlier")
        st.close()


class TestHostCooperation(unittest.TestCase):
    def test_wrapper_keeps_partial_shape_and_is_coroutine(self):
        rec, st, att, host, a, b = setup()
        att.ensure_wrapped()
        import inspect
        for m in host.metas:
            self.assertIsInstance(m.handler, functools.partial)
            self.assertTrue(inspect.iscoroutinefunction(m.handler))     # 宿主 assert 这个
            self.assertTrue(getattr(m.handler.func, "_sf_wrapped", False))
            self.assertEqual(m.handler.args[0].__class__.__name__ in ("PluginA", "PluginB"), True)
        st.close()

    def test_idempotent_no_stacking(self):
        rec, st, att, host, a, b = setup()
        self.assertEqual(att.ensure_wrapped(), 2)
        self.assertEqual(att.ensure_wrapped(), 0)
        self.assertEqual(att.ensure_wrapped(), 0)
        wrapped_func = host.metas[0].handler.func
        self.assertIs(wrapped_func._sf_orig, PluginA.inject)    # 原函数没被套第二层
        st.close()

    def test_plugin_reload_rebinds_new_instance_through_wrapper(self):
        rec, st, att, host, a, b = setup()
        att.ensure_wrapped()
        meta = host.metas[0]
        # 仿宿主插件管理器重载：raw = handler.func；handler = partial(raw, 新实例)
        raw = meta.handler.func
        new_inst = PluginA(tag="新实例")
        meta.handler = functools.partial(raw, new_inst)
        self.assertEqual(att.ensure_wrapped(), 0)                 # 重载后不会再叠一层
        ev = fakes.FakeEvent(span_id="sp-rl")
        req = Req()
        rec.on_request_first(ev, req)
        run(meta.handler(ev, req))
        self.assertIn("<lore>新实例</lore>", req.system_prompt)      # 调到的是新实例，不是旧实例
        self.assertNotIn("设定A", req.system_prompt)
        notes = rec.handler_notes[id(ev)]["notes"]
        self.assertEqual(notes[0]["plugin"], "plugin_a")           # 归属照样记得上
        st.close()

    def test_restore_puts_back_original(self):
        rec, st, att, host, a, b = setup()
        att.ensure_wrapped()
        n = att.restore()
        self.assertEqual(n, 2)
        for m, cls in zip(host.metas, (PluginA, PluginB)):
            self.assertIs(m.handler.func, cls.inject)
            self.assertIsInstance(m.handler.args[0], cls)
        # 还原后再请求也不会再包
        self.assertEqual(att.ensure_wrapped(), 0)
        st.close()

    def test_own_handlers_and_unbound_handlers_skipped(self):
        class Own:
            async def mine(self, event, req):
                return None
        own = make_meta(Own, Own(), "mine", "sf_plugin.main")
        unbound = SimpleNamespace(handler=PluginA.noop, handler_name="noop",
                                  handler_module_path="plugins.a.main", extras_configs={})
        rec, st, att, host, a, b = setup([own, unbound])
        att.ensure_wrapped()
        self.assertFalse(getattr(own.handler.func, "_sf_wrapped", False))   # 自己的不包
        self.assertIs(unbound.handler, PluginA.noop)                         # 没绑实例的不动
        st.close()

    def test_wrapper_from_old_instance_is_replaced(self):
        rec, st, att, host, a, b = setup()
        att.ensure_wrapped()
        rec2, st2, att2, _, _, _ = setup()
        att2._host = host                       # 新实例（会话流重载）接管同一批处理函数
        att2.ensure_wrapped()
        self.assertIs(host.metas[0].handler.func._sf_owner, att2)
        self.assertIs(host.metas[0].handler.func._sf_orig, PluginA.inject)
        st.close()
        st2.close()

    def test_host_missing_marks_unavailable_and_does_not_raise(self):
        rec, st, nm, _ = fakes.make_recorder()

        def broken():
            raise ImportError("宿主没有这个模块")
        att = attrib_mod.Attributor(rec, host=broken)
        self.assertEqual(att.ensure_wrapped(), 0)
        self.assertFalse(att.status["available"])
        self.assertIn("ImportError", att.status["error"])
        st.close()

    def test_not_recorded_events_do_not_accumulate_notes(self):
        rec, st, nm, _ = fakes.make_recorder({"exclude_umos": [fakes.UMO_QQ]})
        a = PluginA()
        host = FakeHost([make_meta(PluginA, a, "inject", "plugins.a.main")])
        att = attrib_mod.Attributor(rec, own_package="sf_plugin", host=host)
        ev = fakes.FakeEvent(umo=fakes.UMO_QQ, span_id="sp-ex")
        req = Req()
        rec.on_request_first(ev, req)
        att.ensure_wrapped()
        run(host.call_hooks(ev, req))
        self.assertEqual(rec.handler_notes, {})
        st.close()


class TestGuesser(unittest.TestCase):
    def test_finds_tag_literal_in_plugin_sources(self):
        root = Path(tempfile.mkdtemp(prefix="sfguess-"))
        (root / "plug_mem").mkdir()
        (root / "plug_mem" / "main.py").write_text('BLOCK = "<RAG-Faiss-Memory>x</RAG-Faiss-Memory>"\n', encoding="utf-8")
        (root / "plug_mem" / "tests").mkdir()
        (root / "plug_mem" / "tests" / "t.py").write_text('"<only_in_tests>"', encoding="utf-8")
        (root / "core").mkdir()
        (root / "core" / "agent.py").write_text('s = "<system_reminder>"\n' * 3, encoding="utf-8")
        (root / "plug_other").mkdir()
        (root / "plug_other" / "m.py").write_text('re.sub("<system_reminder>")\n', encoding="utf-8")
        g = attrib_mod.Guesser([("plug_mem", root / "plug_mem"), ("AstrBot 内核", root / "core"),
                                ("plug_other", root / "plug_other")])
        self.assertEqual(g.guess("RAG-Faiss-Memory"), ["plug_mem"])
        self.assertEqual(g.guess("system_reminder"), ["AstrBot 内核", "plug_other"])   # 次数多的在前
        self.assertEqual(g.guess("only_in_tests"), [])                                  # tests 目录不算
        self.assertEqual(g.guess("系统提示词追加"), [])
        self.assertEqual(g.guess(None), [])

    def test_default_roots_skip_own_dir(self):
        d = Path(tempfile.mkdtemp(prefix="sfroots-"))
        (d / "mine").mkdir()
        (d / "other").mkdir()
        (d / ".hidden").mkdir()
        labels = [l for l, _ in attrib_mod.Guesser.default_roots(d / "mine")]
        self.assertIn("other", labels)
        self.assertNotIn("mine", labels)
        self.assertNotIn(".hidden", labels)


if __name__ == "__main__":
    unittest.main()
