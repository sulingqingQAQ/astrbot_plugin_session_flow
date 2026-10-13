# -*- coding: utf-8 -*-
"""recorder 回放测试：照 P0 时间线走钩子，查库里的时间线。"""

from __future__ import annotations

import unittest

import fakes
from fakes import FakeEvent, FakeReq, FakeCtx, FakeResp, FakeResult
from sf import recorder as recorder_mod


def step_run(rec, new_messages, tool_uses=None, llm_text="好", umo=None, span="s",
             prompt="掷个骰子"):
    return fakes.drive_run(rec, umo=umo or fakes.UMO_WC, span=span, prompt=prompt,
                           new_messages=new_messages, tool_uses=tool_uses,
                           llm_resp_text=llm_text)


class TestConfigShape(unittest.TestCase):
    def test_main_lambda_shape(self):
        # main.py 传的是 3 参 get(cfg,key,default)：联调曾因 2 参 lambda 炸过加载
        cfg = recorder_mod.RecorderConfig.from_config(
            {"enable_record": False}, lambda c, k, d=None: c.get(k, d))
        self.assertFalse(cfg.enable_record)
        self.assertEqual(cfg.max_text_chars, 50000)


CALL = "call_00_test1"


class TestNoTool(unittest.TestCase):
    def test_done_no_tool(self):
        rec, st, _, _ = fakes.make_recorder()
        rid, _ = step_run(rec, [fakes.assistant_msg("掷出来是 8", think="用户要掷骰子")],
                          span="s1")
        row = st.get_run_row(rid)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["n_steps"], 1)
        self.assertEqual(row["n_tools"], 0)
        self.assertEqual(row["reply_text"], "掷出来是 8")
        evs = st.events_for(rid)
        kinds = [e["kind"] for e in evs]
        self.assertEqual(kinds[0], "user")
        self.assertIn("think", kinds)
        self.assertIn("text", kinds)
        st.close()


class TestSingleTool(unittest.TestCase):
    def test_tool_times(self):
        rec, st, _, _ = fakes.make_recorder()
        cur = [1000.0]
        rec.clock = lambda: cur[0]
        ev = FakeEvent(span_id="s2")
        req = FakeReq(prompt="掷个20面骰子", system_prompt="sys")
        rec.on_waiting(ev)
        rec.on_request_first(ev)
        cur[0] = 1001.0
        rec.on_request_last(ev, req)
        msgs = [{"role": "system", "content": "sys"},
                fakes.user_msg("掷个20面骰子",
                               ["<system_reminder>现在时间</system_reminder>"])]
        rec.on_agent_begin(ev, FakeCtx(msgs))
        cur[0] = 1002.0
        rec.on_tool_start(ev, fakes._Tool("sf_probe_dice"), {"sides": 20})
        cur[0] = 1003.5
        rec.on_tool_end(ev, fakes._Tool("sf_probe_dice"), {"sides": 20},
                        fakes._ToolResult("sf_probe_dice 结果：8（20 面）"))
        cur[0] = 1005.0
        rec.on_llm_response(ev, FakeResp("掷出来是 8"))
        msgs.append(fakes.assistant_msg("", think="调工具",
                                        tool_calls=[fakes.tool_call_msg(CALL, "sf_probe_dice", '{"sides": 20}')]))
        msgs.append(fakes.tool_msg("sf_probe_dice 结果：8（20 面）", CALL))
        msgs.append(fakes.assistant_msg("掷出来是 8"))
        rec.on_agent_done(ev, FakeCtx(msgs), FakeResp("掷出来是 8"))
        rid = "s2#1"
        row = st.get_run_row(rid)
        self.assertEqual(row["n_tools"], 1)
        self.assertEqual(row["n_steps"], 2)
        evs = {e["kind"]: e for e in st.events_for(rid) if e["kind"] in ("tool_call", "tool_result")}
        self.assertAlmostEqual(evs["tool_call"]["t"], 1002.0)
        self.assertAlmostEqual(evs["tool_result"]["t"], 1003.5)
        self.assertAlmostEqual(evs["tool_result"]["dur_ms"], 1500.0)
        inj = [e for e in st.events_for(rid) if e["kind"] == "inject"]
        self.assertEqual(inj[0]["name"], "system_reminder")
        st.close()


class TestTwoTools(unittest.TestCase):
    def test_llm_ms_chain(self):
        rec, st, _, _ = fakes.make_recorder()
        cur = [15.29]
        rec.clock = lambda: cur[0]
        ev = FakeEvent(span_id="s3")
        req = FakeReq(prompt="掷两次")
        rec.on_waiting(ev)
        rec.on_request_first(ev)
        rec.on_request_last(ev, req)
        msgs = [{"role": "system", "content": "sys"}, fakes.user_msg("掷两次")]
        rec.on_agent_begin(ev, FakeCtx(msgs))
        cur[0] = 16.95
        rec.on_tool_start(ev, fakes._Tool("dice"), {"sides": 6})
        cur[0] = 18.17
        rec.on_tool_end(ev, fakes._Tool("dice"), {"sides": 6}, fakes._ToolResult("6面：4"))
        cur[0] = 19.93
        rec.on_tool_start(ev, fakes._Tool("dice"), {"sides": 5})
        cur[0] = 21.13
        rec.on_tool_end(ev, fakes._Tool("dice"), {"sides": 5}, fakes._ToolResult("5面：3"))
        cur[0] = 23.03
        rec.on_llm_response(ev, FakeResp("结果 4 和 3"))
        msgs.append(fakes.assistant_msg("", think="第一次",
                                        tool_calls=[fakes.tool_call_msg("c1", "dice", '{"sides": 6}')]))
        msgs.append(fakes.tool_msg("6面：4", "c1"))
        msgs.append(fakes.assistant_msg("", think="第二次",
                                        tool_calls=[fakes.tool_call_msg("c2", "dice", '{"sides": 5}')]))
        msgs.append(fakes.tool_msg("5面：3", "c2"))
        msgs.append(fakes.assistant_msg("结果 4 和 3"))
        rec.on_agent_done(ev, FakeCtx(msgs), FakeResp("结果 4 和 3"))
        import json
        evs = st.events_for("s3#1")
        thinks = [e for e in evs if e["kind"] == "think"]
        llms = [json.loads(e["data"])["llm_ms"] for e in thinks if e["data"]]
        # 第1步 15.29→16.95 ≈1660ms，第2步 18.17→19.93，第3步 21.13→23.03
        self.assertEqual(len(llms), 2)
        self.assertAlmostEqual(llms[0], 1660.0, delta=1.0)
        self.assertAlmostEqual(llms[1], 1760.0, delta=1.0)
        st.close()


class TestToolError(unittest.TestCase):
    def test_error_without_respond(self):
        rec, st, _, _ = fakes.make_recorder()
        rid, _ = step_run(
            rec, [fakes.assistant_msg("", think="调坏工具",
                                      tool_calls=[fakes.tool_call_msg(CALL, "sf_probe_fail", "{}")]),
                  fakes.tool_msg("error: sf_probe_fail 故意抛错：test", CALL),
                  fakes.assistant_msg("工具报错了")],
            tool_uses=[("sf_probe_fail", {}, None)],  # 抛错：没有 tool_respond
            span="s4")
        row = st.get_run_row(rid)
        self.assertEqual(row["n_tools"], 1)
        self.assertEqual(row["n_errors"], 1)
        evs = st.events_for(rid)
        tr = [e for e in evs if e["kind"] == "tool_result"][0]
        self.assertEqual(tr["is_error"], 1)
        st.close()


class TestModelFail(unittest.TestCase):
    def test_decorating_streaming_finish_closes_error(self):
        rec, st, _, _ = fakes.make_recorder()
        ev = FakeEvent(span_id="s8")
        req = FakeReq(prompt="你好")
        rec.on_waiting(ev)
        rec.on_request_last(ev, req)
        msgs = [{"role": "system", "content": "sys"}, fakes.user_msg("你好")]
        rec.on_agent_begin(ev, FakeCtx(msgs))
        ev.set_result(FakeResult("All chat models failed: boom", "STREAMING_FINISH", False))
        closed = rec.on_decorating(ev)
        self.assertTrue(closed)
        row = st.get_run_row("s8#1")
        self.assertEqual(row["status"], "error")
        self.assertIn("All chat models failed", row["error_text"])
        st.close()

    def test_mid_decorating_llm_result_ignored(self):
        rec, st, _, _ = fakes.make_recorder()
        ev = FakeEvent(span_id="s9")
        req = FakeReq(prompt="你好")
        rec.on_waiting(ev)
        rec.on_request_last(ev, req)
        rec.on_agent_begin(ev, FakeCtx([{"role": "system", "content": "s"}, fakes.user_msg("你好")]))
        ev.set_result(FakeResult("", "LLM_RESULT", True))  # 非流式中途 decorating
        self.assertFalse(rec.on_decorating(ev))
        self.assertIsNotNone(st.get_run_row("s9#1"))
        self.assertEqual(st.get_run_row("s9#1")["status"], "running")
        st.close()


class TestInterruptExclude(unittest.TestCase):
    def test_new_run_interrupts_old(self):
        rec, st, _, _ = fakes.make_recorder()
        ev1 = FakeEvent(span_id="sA")
        req = FakeReq(prompt="第一句")
        rec.on_waiting(ev1)
        rec.on_request_last(ev1, req)
        ev2 = FakeEvent(span_id="sB")
        rec.on_waiting(ev2)
        rec.on_request_last(ev2, FakeReq(prompt="第二句"))
        self.assertEqual(st.get_run_row("sA#1")["status"], "interrupted")
        self.assertEqual(st.get_run_row("sB#1")["status"], "running")
        st.close()

    def test_exclude_umos(self):
        rec, st, _, _ = fakes.make_recorder({"exclude_umos": ["default:GroupMessage:*"]})
        ev = FakeEvent(umo=fakes.UMO_QQ, span_id="sX")
        rec.on_waiting(ev)
        self.assertIsNone(rec.on_request_last(ev, FakeReq()))
        self.assertIsNone(st.get_run_row("sX#1"))
        st.close()

    def test_enable_record_off(self):
        rec, st, _, _ = fakes.make_recorder({"enable_record": False})
        ev = FakeEvent(span_id="sY")
        rec.on_waiting(ev)
        self.assertIsNone(rec.on_request_last(ev, FakeReq()))
        st.close()

    def test_hook_exception_does_not_propagate(self):
        rec, st, _, _ = fakes.make_recorder()
        rec.on_waiting(object())  # 脏事件：内部消化，不抛
        rec.on_agent_done(object(), object(), object())
        st.close()

    def test_restart_marks_running_interrupted(self):
        rec, st, _, _ = fakes.make_recorder()
        ev = FakeEvent(span_id="sZ")
        rec.on_waiting(ev)
        rec.on_request_last(ev, FakeReq())
        ids = rec.recover_interrupted()
        self.assertEqual(ids, ["sZ#1"])
        self.assertEqual(st.get_run_row("sZ#1")["status"], "interrupted")
        st.close()

    def test_plugin_error_note(self):
        rec, st, _, _ = fakes.make_recorder()
        # 没有开着的运行：按备注运行记一条（事件真实发生，只是没有对应运行）
        ev = FakeEvent(span_id="sN")
        rec.on_plugin_error(ev, "某插件", "on_x", ValueError("炸"), "tb")
        rows = list(st._read_conn().execute(
            "SELECT * FROM runs WHERE source='note'").fetchall())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "error")
        st.close()

    def test_cid_switch(self):
        rec, st, _, _ = fakes.make_recorder()
        # r1 还开着时同 umo 来了新 cid 的运行：旧轮 interrupted，新轮记新 cid
        ev1 = FakeEvent(span_id="c1")
        rec.on_waiting(ev1)
        rec.on_request_last(ev1, FakeReq(prompt="第一句", cid="cid-1"))
        r2, _ = fakes.drive_run(rec, span="c2", prompt="新对话一句", cid="cid-2",
                                new_messages=[fakes.assistant_msg("回二")])
        self.assertEqual(st.get_run_row("c1#1")["cid"], "cid-1")
        self.assertEqual(st.get_run_row(r2)["cid"], "cid-2")
        self.assertEqual(st.get_run_row("c1#1")["status"], "interrupted")
        st.close()


class TestAlignClosest(unittest.TestCase):
    def test_nearest_stats_wins(self):
        # 联调抓到的 bug：同 cid 两轮前后脚，旧代码 ORDER BY start_time DESC
        # 会把后一轮的 stats 行连到前一轮上。必须按时间最近连。
        import sqlite3
        import time
        rec, st, _, _ = fakes.make_recorder()
        now = time.time()
        umo, cid = fakes.UMO_WC, "cid-x"
        st.insert_run({"run_id": "r1", "umo": umo, "cid": cid, "source": "live",
                       "status": "done", "sort_at": now - 10, "ord": 0, "begin_at": now - 10})
        st.insert_run({"run_id": "r2", "umo": umo, "cid": cid, "source": "live",
                       "status": "done", "sort_at": now - 8, "ord": 0, "begin_at": now - 8})
        adb = fakes.tmp_db_path("-ab.db")
        a = sqlite3.connect(str(adb))
        a.execute("CREATE TABLE provider_stats(id INTEGER PRIMARY KEY, status TEXT, "
                  "provider_id TEXT, provider_model TEXT, token_input_other INTEGER, "
                  "token_input_cached INTEGER, token_output INTEGER, start_time REAL, "
                  "end_time REAL, time_to_first_token REAL, umo TEXT, conversation_id TEXT)")
        a.execute("INSERT INTO provider_stats VALUES (1,'completed','p','m',100,0,10,?,?,1.0,?,?)",
                  (now - 10, now - 9, umo, cid))
        a.execute("INSERT INTO provider_stats VALUES (2,'completed','p','m',200,0,20,?,?,1.0,?,?)",
                  (now - 8, now - 7, umo, cid))
        a.commit()
        a.close()
        out = rec.align_provider_stats(str(adb))
        self.assertEqual(out["linked"], 2)
        self.assertEqual(st.get_run_row("r1")["stats_id"], 1)
        self.assertEqual(st.get_run_row("r2")["stats_id"], 2)
        self.assertEqual(st.get_run_row("r1")["token_in"], 100)
        self.assertEqual(st.get_run_row("r2")["token_in"], 200)
        st.close()


class TestConcurrentDecorating(unittest.TestCase):
    def test_other_event_decorating_does_not_close_running(self):
        # S1：同群 B 的指令回复走 decorating，不能把正在跑工具的 A 关成失败
        rec, st, _, _ = fakes.make_recorder()
        umo = fakes.UMO_QQ
        evA = FakeEvent(umo=umo, span_id="A", mtype="GroupMessage")
        rec.on_waiting(evA)
        rec.on_request_first(evA)
        rec.on_request_last(evA, FakeReq(prompt="帮我掷骰子", cid="c1"))
        msgs = [{"role": "system", "content": "s"}, fakes.user_msg("帮我掷骰子")]
        rec.on_agent_begin(evA, FakeCtx(msgs))
        rec.on_tool_start(evA, fakes._Tool("dice"), {"sides": 6})
        evB = FakeEvent(umo=umo, span_id="B", mtype="GroupMessage", sender_id="s2")
        evB.set_result(FakeResult("指令的回复文本", "GENERAL_RESULT", False))
        self.assertFalse(rec.on_decorating(evB))
        self.assertEqual(st.get_run_row("A#1")["status"], "running")
        rec.on_tool_end(evA, fakes._Tool("dice"), {"sides": 6},
                        fakes._ToolResult("6面：4"))
        rec.on_llm_response(evA, FakeResp("掷出来是4"))
        msgs.append(fakes.assistant_msg(
            "", tool_calls=[fakes.tool_call_msg(CALL, "dice", '{"sides": 6}')]))
        msgs.append(fakes.tool_msg("6面：4", CALL))
        msgs.append(fakes.assistant_msg("掷出来是4"))
        rec.on_agent_done(evA, FakeCtx(msgs), FakeResp("掷出来是4"))
        rowA = st.get_run_row("A#1")
        self.assertEqual(rowA["status"], "done")
        self.assertGreater(rowA["n_steps"], 0)
        self.assertIn("掷出来是4", rowA["reply_text"])
        st.close()

    def test_span_seq_cleaned_after_close(self):
        # S9：运行结束后 _span_seq 要清，不长期涨
        rec, st, _, _ = fakes.make_recorder()
        for i in range(3):
            fakes.drive_run(rec, span=f"q{i}", prompt="x",
                            new_messages=[fakes.assistant_msg("ok")])
        self.assertEqual(len(rec._span_seq), 0)
        self.assertEqual(len(rec.open_by_eid), 0)
        st.close()


def make_stats_db(path, rows):
    import sqlite3
    a = sqlite3.connect(str(path))
    a.execute("CREATE TABLE provider_stats(id INTEGER PRIMARY KEY, status TEXT, "
              "provider_id TEXT, provider_model TEXT, token_input_other INTEGER, "
              "token_input_cached INTEGER, token_output INTEGER, start_time REAL, "
              "end_time REAL, time_to_first_token REAL, umo TEXT, conversation_id TEXT)")
    a.executemany("INSERT INTO provider_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    a.commit()
    a.close()
    return str(path)


def live_run(st, rid, begin, umo=None, cid="cid-x"):
    st.insert_run({"run_id": rid, "umo": umo or fakes.UMO_WC, "cid": cid,
                   "source": "live", "status": "done", "sort_at": begin,
                   "ord": 0, "begin_at": begin})


class TestAlignCases(unittest.TestCase):
    def test_error_stats_flips_run(self):
        # stats error 行：运行改 error、n_errors+1、补 error 事件
        import time
        rec, st, _, _ = fakes.make_recorder()
        now = time.time()
        live_run(st, "a1", now - 10)
        adb = make_stats_db(fakes.tmp_db_path("-s.db"), [
            (7, "error", "p", "m", 0, 0, 0, now - 10, now - 9, None, fakes.UMO_WC, "cid-x")])
        out = rec.align_provider_stats(adb)
        self.assertEqual(out["linked"], 1)
        row = st.get_run_row("a1")
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["n_errors"], 1)
        errs = [e for e in st.events_for("a1") if e["kind"] == "error"]
        self.assertTrue(errs)
        st.close()

    def test_used_stats_not_relinked(self):
        import time
        rec, st, _, _ = fakes.make_recorder()
        now = time.time()
        live_run(st, "b1", now - 10)
        live_run(st, "b2", now - 9)
        adb = make_stats_db(fakes.tmp_db_path("-s.db"), [
            (8, "completed", "p", "m", 50, 0, 5, now - 10, now - 9, 0.5, fakes.UMO_WC, "cid-x")])
        out = rec.align_provider_stats(adb)
        self.assertEqual(out["linked"], 1)
        linked = [r for r in ("b1", "b2") if st.get_run_row(r)["stats_id"] == 8]
        self.assertEqual(len(linked), 1)
        other = "b2" if linked[0] == "b1" else "b1"
        self.assertIsNone(st.get_run_row(other)["stats_id"])
        st.close()

    def test_no_match_leaves_null(self):
        import time
        rec, st, _, _ = fakes.make_recorder()
        now = time.time()
        live_run(st, "c1", now - 10)
        adb = make_stats_db(fakes.tmp_db_path("-s.db"), [
            (9, "completed", "p", "m", 50, 0, 5, now - 1000, now - 999, 0.5, fakes.UMO_WC, "cid-x")])
        out = rec.align_provider_stats(adb)
        self.assertEqual(out["linked"], 0)
        self.assertIsNone(st.get_run_row("c1")["stats_id"])
        st.close()


class TestToolsAvailable(unittest.TestCase):
    def test_names_method_called(self):
        # S2：真实 func_tool.names 是方法不是属性，旧代码 list(names) 被吞掉恒为 []
        import json
        rec, st, _, _ = fakes.make_recorder()
        ev = FakeEvent(span_id="e1")
        rec.on_waiting(ev)
        rec.on_request_first(ev)
        rec.on_request_last(ev, FakeReq(prompt="x", tools=["a", "b", "c"]))
        got = json.loads(st.get_run_row("e1#1")["tools_available"])
        self.assertEqual(got, ["a", "b", "c"])
        st.close()


class TestModelFailCount(unittest.TestCase):
    def test_fail_close_counts_error(self):
        # S4：模型失败的运行 n_errors 要算上这次失败
        rec, st, _, _ = fakes.make_recorder()
        ev = FakeEvent(span_id="m9")
        rec.on_waiting(ev)
        rec.on_request_last(ev, FakeReq(prompt="你好"))
        rec.on_agent_begin(ev, FakeCtx(
            [{"role": "system", "content": "s"}, fakes.user_msg("你好")]))
        ev.set_result(FakeResult("All chat models failed: 炸了", "STREAMING_FINISH", False))
        rec.on_decorating(ev)
        row = st.get_run_row("m9#1")
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["n_errors"], 1)
        st.close()


if __name__ == "__main__":
    unittest.main()
