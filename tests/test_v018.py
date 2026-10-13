# -*- coding: utf-8 -*-
"""v0.1.8：上下文体积构成 / 每轮时间拆分 / 顶部横幅自检 / 按插件统计注入量 / 批量导出 / 导出屏蔽。"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from types import SimpleNamespace

import fakes
from sf import ctxsize
from sf import export as export_mod
from sf import health as health_mod
from sf import mask as mask_mod
from sf import stats as stats_mod
from sf import store as store_mod
from sf import timing as timing_mod
from sf import webapi as webapi_mod

import test_hooks_ext as hx


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def evs(st, run_id, kind=None):
    out = []
    for e in st.events_for(run_id):
        if kind is None or e["kind"] == kind:
            d = dict(e)
            d["data"] = json.loads(d["data"]) if d.get("data") else {}
            out.append(d)
    return out


# =================================================================== 上下文体积构成
class TestCtxsize(unittest.TestCase):
    def test_count_matches_per_char_definition(self):
        # “中文等宽字符” = UTF-8 占 3 字节及以上的字符；用逐字符的定义对一遍
        for s in ["", "abc", "你好abc", "😀a你", "é你", "あいう한글ａ，。", "plain ascii " * 5, "混 合 mixed 😀😀 é"]:
            w = sum(1 for ch in s if len(ch.encode("utf-8")) >= 3)
            self.assertEqual(ctxsize.count(s), (w, len(s) - w), s)
        self.assertEqual(ctxsize.count(None), (0, 0))
        self.assertEqual(sum(ctxsize.count("a\ud800b")), 3)       # 孤立代理字符：退回逐字符数，不抛，总数不丢

    def test_measure_shapes(self):
        ctxs = [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": [{"type": "text", "text": "你好 hello"}, {"type": "image_url", "image_url": {"url": "x"}}]},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "dice", "arguments": '{"n": 6}'}}]},
            {"role": "tool", "content": "掷出 4", "tool_call_id": "c1"},
            {"role": "assistant", "content": [{"type": "think", "think": "想一想"}, {"type": "text", "text": "好的"}]},
            "不是字典的怪消息",
            {"role": "_checkpoint", "content": [{"type": "text", "text": "内部标记" * 50}]},
        ]
        m = ctxsize.measure(ctxs, system_prompt="你是小苏", prompt="问", extras=["<a>x</a>"],
                            tools_text="[]", n_tools=2, image_urls=1)
        self.assertEqual(m["n_msgs"], 6)                              # checkpoint 标记不算一条消息
        self.assertEqual(m["checkpoints"], 1)
        self.assertNotIn("_checkpoint", m["roles"])                   # 也不进各角色的字数里
        self.assertEqual(m["roles"]["user"]["n"], 1)
        self.assertEqual(m["roles"]["user"]["cjk"], 2)               # 你好
        self.assertEqual(m["roles"]["user"]["other"], 6)             # " hello"
        self.assertEqual(m["roles"]["assistant"]["n"], 2)
        self.assertEqual(m["roles"]["assistant"]["tool_calls"], 1)
        self.assertEqual(m["roles"]["assistant"]["cjk"], 5)          # 想一想好的
        self.assertEqual(m["roles"]["other"]["n"], 1)                # 不认识的消息算“其他”，不丢
        self.assertEqual(m["images"], 1)
        self.assertEqual(m["tool_calls"], 1)
        self.assertEqual(m["system_prompt"]["cjk"], 4)
        self.assertEqual(m["prompt"]["n"], 1)
        self.assertEqual(m["extras"]["n"], 1)
        self.assertEqual(m["tools"]["n"], 2)
        self.assertEqual(m["request_images"], 1)
        # 最长的几条按字数排，带开头
        self.assertEqual(m["top"][0]["chars"], max(t["chars"] for t in m["top"]))
        self.assertTrue(all("head" in t for t in m["top"]))
        # 不存历史正文：开头最多 60 字
        long = ctxsize.measure([{"role": "user", "content": "长" * 500}])
        self.assertEqual(len(long["top"][0]["head"]), 60)

    def test_message_objects_are_measured_like_dicts(self):
        # 有的 AstrBot 版本里历史是 pydantic 消息对象而不是字典：不能因此全算成“其他”
        class Msg:
            def __init__(self, role, content):
                self.role, self.content = role, content

            def model_dump(self):
                return {"role": self.role, "content": self.content}

        m = ctxsize.measure([Msg("user", "你好 hi"), Msg("assistant", "好的"), Msg("_checkpoint", "x")])
        self.assertEqual(m["n_msgs"], 2)
        self.assertEqual(m["checkpoints"], 1)
        self.assertEqual(m["roles"]["user"]["cjk"], 2)
        self.assertNotIn("other", m["roles"])

    def test_top_keeps_only_five_longest(self):
        ctxs = [{"role": "user", "content": "字" * (i + 1)} for i in range(30)]
        m = ctxsize.measure(ctxs)
        self.assertEqual([t["chars"] for t in m["top"]], [30, 29, 28, 27, 26])

    def test_tools_json(self):
        class Good:
            def openai_schema(self):
                return [{"type": "function", "function": {"name": "dice", "description": "掷骰子"}}]

        class Bad:
            def openai_schema(self):
                raise RuntimeError("坏了")

        self.assertIn("掷骰子", ctxsize.tools_json(Good()))
        self.assertIsNone(ctxsize.tools_json(Bad()))
        self.assertIsNone(ctxsize.tools_json(object()))              # 这个版本没有该接口 → 页面写“没数到”

    def test_estimate_sums_to_provider_total(self):
        ctx = ctxsize.measure([{"role": "user", "content": "你好" * 100}, {"role": "assistant", "content": "ok " * 100}],
                              system_prompt="系统" * 50, prompt="问", tools_text="x" * 400, n_tools=3)
        est = ctxsize.estimate(ctx, 10000)
        self.assertEqual(est["provider_total"], 10000)
        self.assertEqual(est["checkpoints"], 0)
        self.assertLessEqual(abs(sum(r["tokens"] for r in est["rows"]) - 10000), len(est["rows"]))
        self.assertAlmostEqual(sum(r["share"] for r in est["rows"]), 1.0, places=6)
        self.assertIn("估算", est["note"])
        # 中文字符权重比英文字符大：同样 200 字，中文行占比更高
        rows = {r["key"]: r for r in est["rows"]}
        self.assertGreater(rows["role:user"]["share"], rows["role:assistant"]["share"] * 0.9)

    def test_estimate_without_provider_total_gives_only_counts(self):
        ctx = ctxsize.measure([{"role": "user", "content": "你好"}], system_prompt="s", tools_text="[]", n_tools=0)
        est = ctxsize.estimate(ctx, None)
        self.assertIsNone(est["provider_total"])
        self.assertTrue(all("tokens" not in r for r in est["rows"]))
        self.assertIn("还没对上服务商", est["note"])

    def test_estimate_says_so_when_counts_do_not_match(self):
        ctx = ctxsize.measure([{"role": "user", "content": "你好"}], system_prompt="s", tools_text=None)
        est = ctxsize.estimate(ctx, 500000)                          # 总量比数到的大几万倍：必须写明对不上
        self.assertIn("差得比较多", est["note"])
        self.assertIn("工具定义没数到", est["note"])
        self.assertFalse(est["tools_counted"])

    def test_measure_is_fast_on_big_history(self):
        ctxs = [{"role": ("user", "assistant", "tool")[i % 3], "content": ("你好世界" * 60 + "hello world " * 20)}
                for i in range(2024)]
        t0 = time.perf_counter()
        ctxsize.measure(ctxs, system_prompt="系" * 8000, tools_text="t" * 30000, n_tools=40)
        self.assertLess(time.perf_counter() - t0, 0.5)               # 本机约 10 ms；留足余量防慢机器误报


# =================================================================== 时间拆分
class TestTiming(unittest.TestCase):
    RUN = {"msg_at": 100.0, "received_at": 110.8, "prep_at": 111.0, "begin_at": 119.0, "end_at": 146.0}

    def evs(self, **over):
        run = {"phase": "run", "req_last_at": 118.9, "tool_ms": 2000,
               "calls": [{"hook": "on_llm_request", "plugin": "memory_companion", "handler": "on_llm_request",
                          "n": 1, "total_ms": 5400.0, "max_ms": 5400.0}]}
        run.update(over)
        after = {"phase": "after", "decorating_at": 146.2, "agent_done_at": 146.0,
                 "calls": [{"hook": "on_decorating_result", "plugin": "seg", "handler": "split",
                            "n": 1, "total_ms": 30.0, "max_ms": 30.0}]}
        return [{"kind": "timing", "data": run}, {"kind": "timing", "data": after}]

    def test_segments_add_up(self):
        t = timing_mod.build(self.RUN, self.evs())
        segs = {s["key"]: s["s"] for s in t["segments"]}
        self.assertAlmostEqual(segs["wait"], 10.8, places=3)
        self.assertAlmostEqual(segs["prep"], 0.2, places=3)
        self.assertAlmostEqual(segs["hooks"], 7.9, places=3)
        self.assertAlmostEqual(segs["start"], 0.1, places=3)
        self.assertAlmostEqual(segs["tools"], 2.0, places=3)
        self.assertAlmostEqual(segs["model"], 25.0, places=3)         # 27 s 的 agent 运行减去 2 s 工具
        self.assertAlmostEqual(segs["after"], 0.2, places=3)
        self.assertAlmostEqual(t["total_s"], 146.2 - 100.0, places=2)  # 首尾相接 = 消息时间 → 发送前
        self.assertEqual(t["calls"][0]["plugin"], "memory_companion")  # 按总耗时排
        self.assertEqual(t["calls"][0]["hook_label"], "请求前")
        self.assertEqual(t["calls"][1]["hook_label"], "发送前")

    def test_missing_points_drop_segments_not_invent_them(self):
        t = timing_mod.build({"received_at": 110.8, "prep_at": 111.0, "begin_at": 119.0, "end_at": 146.0},
                             self.evs())
        keys = [s["key"] for s in t["segments"]]
        self.assertNotIn("wait", keys)                                # 没有平台时间戳就没有这一段
        self.assertIn("prep", keys)

    def test_clock_skew_clamped_and_said(self):
        t = timing_mod.build({**self.RUN, "msg_at": 120.0}, self.evs())
        self.assertEqual(next(s for s in t["segments"] if s["key"] == "wait")["s"], 0.0)
        self.assertTrue(any("时钟" in n for n in t["notes"]))

    def test_no_after_event_is_said(self):
        t = timing_mod.build(self.RUN, self.evs()[:1])
        self.assertNotIn("after", [s["key"] for s in t["segments"]])
        self.assertTrue(any("没有“回复后处理”" in n for n in t["notes"]))

    def test_none_without_timing_events(self):
        self.assertIsNone(timing_mod.build(self.RUN, [{"kind": "inject"}]))


# =================================================================== 记录侧：ctx / timing 事件
class TestRecording(unittest.TestCase):
    def test_ctx_and_timing_events_written(self):
        rec, st, att, host = hx.make({"on_llm_request": [hx.meta(hx.Hist, hx.Hist(), "trim", "plugins.hist.main", 3)],
                                      "on_decorating_result": [hx.meta(hx.Deco, hx.Deco(), "noop", "plugins.deco.main")]})
        hx.drive(rec, att, host)
        rid = "sp-x#1"
        rows = evs(st, rid)
        seqs = [r["seq"] for r in rows]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)), "事件序号不能重复")
        ctx = evs(st, rid, "ctx")
        self.assertEqual(len(ctx), 1)
        self.assertEqual(ctx[0]["seq"], 2)                            # 紧跟在提问后面
        self.assertEqual(ctx[0]["text"], "")                          # 不进全文索引
        # 量的是最后一个钩子看到的历史：trim 之后只剩 2 条
        self.assertEqual(ctx[0]["data"]["n_msgs"], 2)
        self.assertEqual(ctx[0]["data"]["system_prompt"]["cjk"], 4)   # “你是助手”
        timings = evs(st, rid, "timing")
        by_phase = {t["data"]["phase"]: t["data"] for t in timings}
        self.assertEqual(set(by_phase), {"run", "after"})
        calls = {(c["hook"], c["plugin"], c["handler"]): c for c in by_phase["run"]["calls"]}
        self.assertIn(("on_llm_request", "plugin_hist", "trim"), calls)
        self.assertEqual(calls[("on_llm_request", "plugin_hist", "trim")]["n"], 1)
        self.assertGreaterEqual(by_phase["run"]["req_last_at"], by_phase["run"]["prep_at"])
        after_calls = {(c["hook"], c["plugin"], c["handler"]) for c in by_phase["after"]["calls"]}
        self.assertIn(("on_decorating_result", "plugin_deco", "noop"), after_calls)
        self.assertGreaterEqual(by_phase["after"]["decorating_at"], by_phase["after"]["agent_done_at"])
        st.close()

    def test_unchanged_reply_still_gets_after_timing_but_no_sent(self):
        rec, st, att, host = hx.make({"on_decorating_result": [hx.meta(hx.Deco, hx.Deco(), "noop", "plugins.deco.main")]})
        hx.drive(rec, att, host, reply="好")
        self.assertEqual(evs(st, "sp-x#1", "sent"), [])
        self.assertEqual([t["data"]["phase"] for t in evs(st, "sp-x#1", "timing")].count("after"), 1)
        st.close()

    def test_changed_reply_writes_timing_then_sent_in_order(self):
        rec, st, att, host = hx.make({"on_decorating_result": [hx.meta(hx.Deco, hx.Deco(), "suffix", "plugins.deco.main")]})
        hx.drive(rec, att, host, reply="好")
        rows = evs(st, "sp-x#1")
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds[-2:], ["timing", "sent"])
        self.assertEqual(len({r["seq"] for r in rows}), len(rows))
        st.close()

    def test_every_hook_call_is_timed_even_without_changes(self):
        # 没改任何东西的处理函数也要计时（时间拆分要看谁慢，不只看谁改了东西）
        rec, st, att, host = hx.make({"on_llm_request": [hx.meta(hx.Misc, hx.Misc(), "noop", "plugins.misc.main")]})
        hx.drive(rec, att, host)
        calls = evs(st, "sp-x#1", "timing")[0]["data"]["calls"]
        self.assertEqual([(c["plugin"], c["handler"]) for c in calls], [("plugin_misc", "noop")])
        st.close()

    def test_tool_hooks_during_run_are_timed(self):
        rec, st, att, host = hx.make({"on_llm_response": [hx.meta(hx.Resp, hx.Resp(), "noop", "plugins.resp.main")]})
        hx.drive(rec, att, host)
        calls = [c for t in evs(st, "sp-x#1", "timing") for c in t["data"]["calls"]]
        self.assertIn(("on_llm_response", "plugin_resp", "noop"), {(c["hook"], c["plugin"], c["handler"]) for c in calls})
        st.close()

    def test_excluded_umo_leaves_no_temp_state(self):
        rec, st, nm, _ = fakes.make_recorder({"exclude_umos": [fakes.UMO_WC]})
        ev = fakes.FakeEvent()
        rec.add_handler_timing(ev, {"hook": "on_waiting_llm_request", "plugin": "p", "handler": "h", "ms": 1.0})
        self.assertIn(id(ev), rec.pre_calls)
        rec.on_request_last(ev, fakes.FakeReq())
        self.assertNotIn(id(ev), rec.pre_calls)
        st.close()

    def test_agg_calls_merges_repeats(self):
        a = [{"hook": "h", "plugin": "p", "handler": "f", "ms": 10.0}, {"hook": "h", "plugin": "p", "handler": "f", "ms": 30.0},
             {"hook": "h", "plugin": "q", "handler": "g", "ms": 5.0}]
        out = hx.fakes.recorder_mod.Recorder.agg_calls(a)
        self.assertEqual(out[0], {"hook": "h", "plugin": "p", "handler": "f", "n": 2, "total_ms": 40.0, "max_ms": 30.0})
        self.assertEqual(out[1]["plugin"], "q")

    def test_ctx_failure_is_recorded_not_raised(self):
        rec, st, att, host = hx.make({})
        orig = ctxsize.measure
        ctxsize.measure = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("量不了"))
        try:
            hx.drive(rec, att, host)
        finally:
            ctxsize.measure = orig
        ctx = evs(st, "sp-x#1", "ctx")
        self.assertIn("量不了", ctx[0]["data"]["error"])
        self.assertEqual(evs(st, "sp-x#1", "user")[0]["seq"], 1)      # 运行本身照常记
        st.close()


# =================================================================== 顶部横幅
class TestHealth(unittest.TestCase):
    NOW = 10_000.0

    def test_quiet_when_all_fine(self):
        rt = {"attribution": {"status": {"available": True}}, "tasks": [{"name": "sf-writer", "done": False}],
              "write_queue": 0, "write": {"failed": 0, "busy_since": None}}
        self.assertEqual(health_mod.evaluate(rt, {}, self.NOW), [])

    def test_null_is_not_a_fault(self):
        # 还没有请求经过：available 是 None，不能亮
        self.assertEqual(health_mod.evaluate({"attribution": {"status": {"available": None}}}, {}, self.NOW), [])
        self.assertEqual(health_mod.evaluate({}, {}, self.NOW), [])
        self.assertEqual(health_mod.evaluate(None, None, self.NOW), [])

    def test_each_fault_lights_up(self):
        codes = lambda rt, hooks=None: {p["code"].split(":")[0] for p in health_mod.evaluate(rt, hooks or {}, self.NOW)}
        self.assertEqual(codes({"attribution": {"status": {"available": False, "error": "接口变了"}}}), {"attrib_unavailable"})
        self.assertEqual(codes({"tasks": [{"name": "sf-writer", "done": True, "cancelled": False, "error": "炸了"}]}), {"task_dead"})
        self.assertEqual(codes({"tasks": [{"name": "sf-writer", "done": True, "cancelled": True}]}), set())   # 正常取消不算
        self.assertEqual(codes({}, {"agent_done": {"errors": 2, "last_error": "X", "last_error_ts": self.NOW - 60}}), {"hook_errors"})
        self.assertEqual(codes({}, {"agent_done": {"errors": 2, "last_error": "X", "last_error_ts": self.NOW - 7200}}), set())  # 太久以前
        self.assertEqual(codes({"write_queue": 500}), {"write_stuck"})
        self.assertEqual(codes({"write": {"busy_since": self.NOW - 120}, "write_queue": 1}), {"write_stuck"})
        self.assertEqual(codes({"write": {"failed": 3, "last_fail_at": self.NOW - 30, "last_fail": "locked"}}), {"write_failed"})
        self.assertEqual(codes({"write": {"failed": 3, "last_fail_at": self.NOW - 9000, "last_fail": "locked"}}), set())

    def test_messages_are_plain_language(self):
        ps = health_mod.evaluate({"attribution": {"status": {"available": False, "error": "x"}}}, {}, self.NOW)
        self.assertIn("聊天记录本身还在正常记", ps[0]["text"])
        self.assertEqual(ps[0]["level"], "warn")

    def test_hook_error_timestamp_recorded_by_diag(self):
        from sf import diag as diag_mod
        d = diag_mod.DiagLog(None, enabled=True)
        with d.guard("agent_done", ""):
            raise RuntimeError("炸")
        h = d.hook_stats()["agent_done"]
        self.assertEqual(h["errors"], 1)
        self.assertIsNotNone(h["last_error_ts"])


# =================================================================== 造数据：几轮带注入的回合
SP_A = "你是小苏。\n" + "甲" * 200
SP_B = "你是小苏（改）。\n" + "乙" * 200


def seed_runs(st, n=3, umo=fakes.UMO_QQ):
    base = time.time() - 3600
    for i in range(n):
        h = ("aa" if i < 2 else "bb") * 32
        st.put_blob(h, SP_A if i < 2 else SP_B)
        rid = f"r{i}#1"
        st.insert_run({"run_id": rid, "umo": umo, "cid": "c", "source": "live", "status": "done",
                       "sort_at": base + i * 10, "ord": 1, "received_at": base + i * 10 + 0.5,
                       "begin_at": base + i * 10 + 1, "end_at": base + i * 10 + 5, "sender_id": "99887766",
                       "sender_name": "小明", "provider_id": "p", "model": "m", "user_text": f"问题{i}",
                       "reply_text": f"回答{i}", "sysprompt_hash": h, "contexts_len": 4, "n_steps": 1, "n_tools": 1,
                       "token_in": 100, "token_cached": 900, "token_out": 20, "tools_available": ["dice"]})
        st.insert_events([
            {"run_id": rid, "seq": 1, "t": base + i * 10, "kind": "user", "text": f"问题{i}"},
            {"run_id": rid, "seq": 2, "t": base + i * 10, "kind": "ctx", "step": 0, "name": "ctx", "text": "",
             "data": ctxsize.measure([{"role": "user", "content": "你好" * 10}], system_prompt=SP_A, tools_text="[]", n_tools=1)},
            {"run_id": rid, "seq": 3, "t": base + i * 10, "kind": "inject", "step": 0, "name": "group_recent",
             "text": f"群里别人说的话{i}" * 10, "data": {"source": "system_prompt", "attrib": "exact",
                                                        "plugin": "plugin_lingxi", "handler": "on_llm_request"}},
            {"run_id": rid, "seq": 4, "t": base + i * 10, "kind": "inject", "step": 0, "name": "persona",
             "text": "人设" * 5, "data": {"source": "system_prompt", "attrib": "earlier"}},
            {"run_id": rid, "seq": 5, "t": base + i * 10 + 1, "kind": "tool_call", "step": 1, "name": "dice",
             "call_id": "c1", "text": "{}"},
            {"run_id": rid, "seq": 6, "t": base + i * 10 + 2, "kind": "tool_result", "step": 1, "name": "dice",
             "call_id": "c1", "text": "6"},
            {"run_id": rid, "seq": 7, "t": base + i * 10 + 5, "kind": "text", "text": f"回答{i}"},
            {"run_id": rid, "seq": 8, "t": base + i * 10, "kind": "timing", "step": 0, "name": "timing", "text": "",
             "data": {"v": 1, "phase": "run", "msg_at": base + i * 10 - 3, "received_at": base + i * 10 + 0.5,
                      "prep_at": base + i * 10 + 0.6, "req_last_at": base + i * 10 + 0.9, "begin_at": base + i * 10 + 1,
                      "end_at": base + i * 10 + 5, "tool_ms": 1000, "calls": []}},
        ])


def make_api(n=3):
    st = store_mod.Store(fakes.tmp_db_path())
    seed_runs(st, n)
    api = webapi_mod.SessionFlowWebApi(st, None)
    api.plugin_version = "v0.1.8-test"
    return st, api


# =================================================================== 按插件统计注入量
class TestInjectStats(unittest.TestCase):
    def test_group_by_plugin_with_shares(self):
        st, api = make_api()
        r = run(api.handle_inject_stats({"days": "0"}, {}))["data"]
        by = {x["label"]: x for x in r["rows"]}
        self.assertEqual(set(by), {"plugin_lingxi", "内核 / 更早"})
        lx = by["plugin_lingxi"]
        self.assertEqual(lx["runs"], 3)
        self.assertEqual(lx["n"], 3)
        self.assertEqual(lx["chars"], sum(len(f"群里别人说的话{i}" * 10) for i in range(3)))
        self.assertEqual(lx["by_source"], {"system_prompt": lx["chars"]})
        self.assertEqual(lx["names"][0][0], "group_recent")
        self.assertEqual(lx["avg_per_run"], round(lx["chars"] / 3))
        self.assertAlmostEqual(sum(x["share"] for x in r["rows"]), 1.0, places=2)
        self.assertEqual(r["rows"][0]["label"], "plugin_lingxi")       # 按字数从大到小
        self.assertEqual(r["live_runs"], 3)
        self.assertAlmostEqual(lx["presence"], 1.0)
        st.close()

    def test_window_and_umo_filters(self):
        st, api = make_api()
        old = time.time() - 40 * 86400
        st.insert_run({"run_id": "old#1", "umo": "default:GroupMessage:other", "source": "live", "status": "done",
                       "sort_at": old, "ord": 1})
        st.insert_events([{"run_id": "old#1", "seq": 1, "t": old, "kind": "inject", "name": "x", "text": "旧" * 100,
                           "data": {"attrib": "exact", "plugin": "plugin_old", "source": "prompt"}}])
        everything = {x["label"] for x in run(api.handle_inject_stats({"days": "0"}, {}))["data"]["rows"]}
        self.assertIn("plugin_old", everything)
        recent = {x["label"] for x in run(api.handle_inject_stats({"days": "30"}, {}))["data"]["rows"]}
        self.assertNotIn("plugin_old", recent)
        one = run(api.handle_inject_stats({"days": "0", "umo": fakes.UMO_QQ}, {}))["data"]
        self.assertNotIn("plugin_old", {x["label"] for x in one["rows"]})
        st.close()

    def test_unattributed_and_guess_labels(self):
        st, api = make_api(1)
        st.insert_events([
            {"run_id": "r0#1", "seq": 20, "kind": "inject", "name": "mem_tag", "text": "甲" * 50, "data": {"attrib": "unattributed"}},
            {"run_id": "r0#1", "seq": 21, "kind": "inject", "name": "other_tag", "text": "乙" * 50, "data": {"attrib": "unattributed"}}])
        api.guesser = SimpleNamespace(guess=lambda name, limit=3: ["plugin_mem"] if name == "mem_tag" else [])
        rows = {x["label"]: x for x in run(api.handle_inject_stats({"days": "0"}, {}))["data"]["rows"]}
        self.assertEqual(rows["疑似 plugin_mem"]["kind"], "guess")
        self.assertEqual(rows["未归属"]["kind"], "unattributed")
        st.close()

    def test_edit_stats_count_removed_history(self):
        st, api = make_api(1)
        st.insert_events([{"run_id": "r0#1", "seq": 30, "kind": "edit", "name": "历史",
                           "text": "历史 20 → 2 条\n删去 18 条：…", "data": {"attrib": "exact", "plugin": "plugin_cost", "what": "历史"}}])
        r = run(api.handle_inject_stats({"days": "0"}, {}))["data"]
        self.assertEqual(r["edits"][0], {"plugin": "plugin_cost", "what": "历史", "n": 1, "runs": 1, "removed": 18})
        st.close()

    def test_fallback_without_json_functions(self):
        st, api = make_api(1)
        conn = st._read_conn()
        rows = stats_mod._rows(conn, "SELECT no_such_function(1)", "SELECT e.run_id, e.name, LENGTH(e.text), e.data "
                               "FROM events e WHERE e.kind=?", ["inject"])
        self.assertEqual(rows[0][3:5], ("exact", "plugin_lingxi"))
        st.close()


# =================================================================== 屏蔽
class TestMask(unittest.TestCase):
    def test_clean_rules(self):
        raw = [{"kind": "inject", "name": " Group_Recent ", "on": True}, {"kind": "inject", "name": "group_recent"},
               {"kind": "bogus", "name": "x"}, {"kind": "tool", "name": "*"}, {"kind": "user", "name": "ignored"},
               "不是字典", {"kind": "edit", "name": "p", "on": False}]
        out = mask_mod.clean_rules(raw)
        self.assertEqual(out, [{"kind": "inject", "name": "Group_Recent", "on": True},
                               {"kind": "tool", "name": "", "on": True},
                               {"kind": "user", "name": "", "on": True},
                               {"kind": "edit", "name": "p", "on": False}])
        self.assertEqual(mask_mod.clean_rules("x"), [])
        self.assertLessEqual(len(mask_mod.clean_rules([{"kind": "tool", "name": f"t{i}"} for i in range(500)])), mask_mod.MAX_RULES)

    def test_no_rules_changes_nothing(self):
        st, api = make_api(1)
        payload, _ = api._run_payload("r0#1")
        out, n = mask_mod.apply(payload, [])
        self.assertIs(out, payload)
        self.assertEqual(n, 0)
        r = run(api.handle_export({"id": "r0#1"}, {}))["data"]
        self.assertEqual(r["masked_segments"], 0)
        self.assertNotIn("被屏蔽", r["text"])
        self.assertIn("群里别人说的话0", r["text"])                    # 默认什么都不屏蔽
        st.close()

    def test_rule_by_name_and_by_plugin_delete_whole_segment(self):
        st, api = make_api(1)
        for rule_name in ("group_recent", "PLUGIN_LINGXI", "plugin_ling*"):   # 注入块名 / 来源插件 / 通配，不分大小写
            mask_mod.save_rules(st, [{"kind": "inject", "name": rule_name}])
            d = run(api.handle_export({"id": "r0#1"}, {}))["data"]
            self.assertEqual(d["masked_segments"], 1, rule_name)
            self.assertNotIn("群里别人说的话", d["text"])
            self.assertIn("有 1 段被屏蔽", d["text"])
            self.assertIn("人设", d["text"])                          # 别的注入不受影响
            self.assertEqual(d["injects"], 1)                         # 屏蔽的不计入注入一览
            self.assertEqual(d["inject_chars"], len("人设" * 5))
        st.close()

    def test_no_placeholder_left_behind(self):
        st, api = make_api(1)
        mask_mod.save_rules(st, [{"kind": "inject", "name": "group_recent"}])
        d = run(api.handle_export({"id": "r0#1", "fmt": "json"}, {}))["data"]
        b = json.loads(d["text"])
        self.assertEqual(b["masked_segments"], 1)
        self.assertEqual([e["seq"] for e in b["events"] if e["kind"] == "inject"], [4])   # 3 号被整条删掉
        self.assertNotIn("已屏蔽", d["text"])
        self.assertNotIn("群里别人说的话", d["text"])
        st.close()

    def test_tool_rule_removes_call_and_result(self):
        st, api = make_api(1)
        mask_mod.save_rules(st, [{"kind": "tool", "name": "dice"}])
        b = json.loads(run(api.handle_export({"id": "r0#1", "fmt": "json"}, {}))["data"]["text"])
        self.assertFalse([e for e in b["events"] if e["kind"] in ("tool_call", "tool_result")])
        self.assertEqual(b["masked_segments"], 2)
        st.close()

    def test_off_rule_does_not_apply_and_mask_zero_bypasses(self):
        st, api = make_api(1)
        mask_mod.save_rules(st, [{"kind": "inject", "name": "group_recent", "on": False}])
        self.assertEqual(run(api.handle_export({"id": "r0#1"}, {}))["data"]["masked_segments"], 0)
        mask_mod.save_rules(st, [{"kind": "inject", "name": "group_recent", "on": True}])
        self.assertEqual(run(api.handle_export({"id": "r0#1"}, {}))["data"]["masked_segments"], 1)
        # 这一次不用规则（导出栏取消勾选）
        self.assertEqual(run(api.handle_export({"id": "r0#1", "mask": "0"}, {}))["data"]["masked_segments"], 0)
        st.close()

    def test_one_time_kinds_and_system_prompt(self):
        st, api = make_api(1)
        d = run(api.handle_export({"id": "r0#1", "mask_kinds": "system_prompt,user"}, {}))["data"]
        self.assertNotIn(SP_A, d["text"])                              # system prompt 整段没了
        self.assertNotIn("## system prompt", d["text"])                # 连标题也不留
        self.assertEqual(d["system_prompt_chars"], 0)
        self.assertNotIn("问题0", d["text"])                           # 提问：事件和运行记录里的那份副本都清掉
        self.assertEqual(d["masked_segments"], 2)                      # 1 条提问 + system prompt
        self.assertEqual(mask_mod.load_rules(st), [])                  # 一次性的，不进库
        st.close()

    def test_masked_inject_also_removed_from_system_prompt_text(self):
        st, api = make_api(1)
        leak = "群里别人说的话0" * 10
        h = "cc" * 32
        st.put_blob(h, "开头\n" + leak + "\n结尾")
        st.update_run("r0#1", {"sysprompt_hash": h})
        mask_mod.save_rules(st, [{"kind": "inject", "name": "group_recent"}])
        d = run(api.handle_export({"id": "r0#1"}, {}))["data"]
        self.assertNotIn("群里别人说的话", d["text"])                   # 注入也写在 system prompt 里，不能换个地方露出来
        self.assertIn("开头", d["text"])
        st.close()

    def test_rules_endpoint_roundtrip_and_suggestions(self):
        st, api = make_api(1)
        r = run(api.handle_mask_rules_save({}, {"rules": [{"kind": "inject", "name": "group_recent"}]}))
        self.assertEqual(r["status"], "ok")
        got = run(api.handle_mask_rules({}, {}))["data"]
        self.assertEqual(got["rules"], [{"kind": "inject", "name": "group_recent", "on": True}])
        self.assertEqual([k["kind"] for k in got["kinds"]], list(mask_mod.KINDS))
        names = {x["name"] for x in got["seen"]["inject"]}
        self.assertIn("group_recent", names)
        self.assertIn("plugin_lingxi", names)                          # 来源插件也在提示里
        self.assertIn("dice", {x["name"] for x in got["seen"]["tool"]})
        self.assertEqual(run(api.handle_mask_rules_save({}, {"rules": "x"}))["status"], "error")
        st.close()


# =================================================================== 批量导出
class TestExportMany(unittest.TestCase):
    def test_route_registered(self):
        names = {r for r, _, _ in webapi_mod.SessionFlowWebApi(None, None).routes()}
        self.assertTrue({"api/export_many", "api/mask_rules", "api/mask_rules/save", "api/stats/inject",
                         "api/health"} <= names)

    def test_page_export_chronological_with_toc_and_one_shared_system_prompt(self):
        st, api = make_api(3)
        r = run(api.handle_export_many({"umo": fakes.UMO_QQ, "page": "1", "size": "20"}, {}))
        self.assertEqual(r["status"], "ok", r)
        d = r["data"]
        md = d["text"]
        self.assertEqual((d["included"], d["skipped"], d["requested"]), (3, 0, 3))
        self.assertRegex(d["filename"], r"^session_flow_runs_\d{8}-\d{6}_3rounds\.md$")
        self.assertIn("## 目录", md)
        self.assertLess(md.index("问题0"), md.index("问题1"))
        self.assertLess(md.index("问题1"), md.index("问题2"))          # 按时间从早到晚（列表是新的在前，文件反过来）
        # 前两轮同一份 system prompt，第三轮另一份：附录里各一份，共两份，正文里不重复贴
        self.assertEqual(md.count(SP_A), 1)
        self.assertEqual(md.count(SP_B), 1)
        self.assertEqual(d["system_prompts"], 2)
        self.assertIn("## system prompt 附录", md)
        self.assertIn("见文件末尾的附录 S1", md)
        self.assertNotIn("100000999", md)                              # 会话 ID 默认打码
        self.assertNotIn("99887766", md)
        st.close()

    def test_json_batch(self):
        st, api = make_api(3)
        d = run(api.handle_export_many({"umo": fakes.UMO_QQ, "fmt": "json"}, {}))["data"]
        b = json.loads(d["text"])
        self.assertEqual(b["format"], "session_flow_runs/1")
        self.assertEqual(len(b["runs"]), 3)
        self.assertEqual(len(b["system_prompts"]), 2)
        self.assertTrue(all(r["system_prompt"].get("text") is None for r in b["runs"]))   # 全文只在顶层放一份
        self.assertNotIn("99887766", d["text"])
        st.close()

    def test_search_scope_and_paging(self):
        st, api = make_api(3)
        d = run(api.handle_export_many({"umo": fakes.UMO_QQ, "scope": "search", "q": "问题1"}, {}))
        self.assertEqual(d["status"], "ok", d)
        self.assertEqual(d["data"]["included"], 1)
        self.assertIn("搜索结果", d["data"]["scope"])
        self.assertNotEqual(run(api.handle_export_many({"umo": fakes.UMO_QQ, "scope": "search"}, {}))["status"], "ok")  # 没条件
        page = run(api.handle_export_many({"umo": fakes.UMO_QQ, "page": "2", "size": "2"}, {}))["data"]
        self.assertEqual(page["included"], 1)                          # 3 轮每页 2 条，第 2 页只剩 1 轮
        st.close()

    def test_size_cap_says_what_was_left_out(self):
        st, api = make_api(3)
        orig = export_mod.MAX_BATCH_CHARS
        export_mod.MAX_BATCH_CHARS = 3500                              # 比一轮的文字略多，放不下三轮
        try:
            d = run(api.handle_export_many({"umo": fakes.UMO_QQ}, {}))["data"]
        finally:
            export_mod.MAX_BATCH_CHARS = orig
        self.assertLess(d["included"], 3)
        self.assertGreaterEqual(d["included"], 1)
        self.assertEqual(d["included"] + d["skipped"], 3)
        self.assertIn("没放进来", d["text"])
        st.close()

    def test_masking_applies_and_counts_in_header(self):
        st, api = make_api(3)
        mask_mod.save_rules(st, [{"kind": "inject", "name": "group_recent"}])
        d = run(api.handle_export_many({"umo": fakes.UMO_QQ}, {}))["data"]
        self.assertEqual(d["masked_segments"], 3)
        self.assertIn("有 3 段被屏蔽", d["text"])
        self.assertNotIn("群里别人说的话", d["text"])
        st.close()

    def test_errors(self):
        st, api = make_api(1)
        self.assertEqual(run(api.handle_export_many({}, {}))["status"], "error")
        self.assertEqual(run(api.handle_export_many({"umo": "no:such:umo"}, {}))["status"], "error")
        st.close()


# =================================================================== 单轮详情里的两块新数据
class TestRunPayload(unittest.TestCase):
    def test_handle_run_carries_context_and_timing_but_not_raw_meta_events(self):
        st, api = make_api(1)
        d = run(api.handle_run({"id": "r0#1"}, {}))["data"]
        self.assertEqual(set(d), {"run", "events", "sysprompt", "context", "timing"})
        self.assertFalse([e for e in d["events"] if e["kind"] in ("ctx", "timing")])   # 不混进时间线
        ctx = d["context"]
        self.assertEqual(ctx["provider_total"], 1000)                   # 100 未命中 + 900 命中缓存
        self.assertEqual(ctx["cache_rate"], 0.9)
        self.assertLessEqual(abs(sum(r["tokens"] for r in ctx["rows"]) - 1000), len(ctx["rows"]))
        self.assertTrue(ctx["tools_counted"])
        tm = d["timing"]
        self.assertAlmostEqual(next(s for s in tm["segments"] if s["key"] == "tools")["s"], 1.0, places=2)
        st.close()

    def test_old_run_without_new_events_has_none(self):
        st, api = make_api(1)
        st.insert_run({"run_id": "legacy#1", "umo": fakes.UMO_QQ, "source": "legacy", "status": "legacy", "sort_at": 1.0})
        d = run(api.handle_run({"id": "legacy#1"}, {}))["data"]
        self.assertIsNone(d["context"])
        self.assertIsNone(d["timing"])
        st.close()

    def test_export_has_both_sections_and_hides_meta_from_timeline(self):
        st, api = make_api(1)
        md = run(api.handle_export({"id": "r0#1"}, {}))["data"]["text"]
        self.assertIn("## 上下文体积构成（估算）", md)
        self.assertIn("## 时间拆分", md)
        self.assertIn("缓存命中率 90.0%", md)
        self.assertNotIn("### #2 ctx", md)
        self.assertNotIn("timing", md.split("## 时间线")[1])
        st.close()

    def test_health_endpoint(self):
        st, api = make_api(1)
        self.assertEqual(run(api.handle_health({}, {}))["data"]["problems"], [])
        api.health_probe = lambda: [{"code": "x", "level": "error", "title": "t", "text": "z"}]
        self.assertEqual(run(api.handle_health({}, {}))["data"]["problems"][0]["code"], "x")

        def boom():
            raise RuntimeError("自检炸了")
        api.health_probe = boom
        p = run(api.handle_health({}, {}))["data"]["problems"][0]
        self.assertEqual(p["code"], "health_failed")
        st.close()


if __name__ == "__main__":
    unittest.main()
