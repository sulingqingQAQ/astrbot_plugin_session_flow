# -*- coding: utf-8 -*-
"""search.parse_query + search 离线用例：结构条件与自由词。"""

from __future__ import annotations

import unittest

import fakes
from sf import search as search_mod

CALL = "call_1"


def seed(rec):
    r_tool, _ = fakes.drive_run(
        rec, span="q1", prompt="掷个骰子", cid="cid-1",
        tool_uses=[("dice", '{"sides": 6}', "6面")],
        new_messages=[
            fakes.assistant_msg("", tool_calls=[fakes.tool_call_msg(CALL, "dice", '{"sides": 6}')]),
            fakes.tool_msg("6面", CALL),
            fakes.assistant_msg("掷出来是6")],
        llm_resp_text="掷出来是6")
    r_err, _ = fakes.drive_run(
        rec, span="q2", prompt="试试坏工具", cid="cid-1",
        tool_uses=[("sf_probe_fail", "{}", None)],
        new_messages=[
            fakes.assistant_msg("", tool_calls=[fakes.tool_call_msg(CALL, "sf_probe_fail", "{}")]),
            fakes.tool_msg("error: 炸了", CALL),
            fakes.assistant_msg("工具报错了")],
        llm_resp_text="工具报错了")
    r_plain, _ = fakes.drive_run(
        rec, span="q3", prompt="今天天气", cid="cid-1",
        new_messages=[fakes.assistant_msg("今天晴")],
        llm_resp_text="今天晴")
    return r_tool, r_err, r_plain


class TestParseQuery(unittest.TestCase):
    def test_split(self):
        p = search_mod.parse_query("tool:dice err:炸 user:你好 umo:webchat 自由词")
        self.assertEqual(p["tools"], ["dice"])
        self.assertEqual(p["errors"], ["炸"])
        self.assertEqual(p["users"], ["你好"])
        self.assertEqual(p["umos"], ["webchat"])
        self.assertEqual(p["texts"], ["自由词"])
        self.assertFalse(p["err_any"])

    def test_bare_err(self):
        p = search_mod.parse_query("err:")
        self.assertTrue(p["err_any"])


class TestSearch(unittest.TestCase):
    def test_tool(self):
        rec, st, nm, _ = fakes.make_recorder()
        r_tool, _, r_plain = seed(rec)
        res = search_mod.search(st, nm, "tool:dice")
        ids = {h["run_id"] for h in res["hits"]}
        self.assertIn(r_tool, ids)
        self.assertNotIn(r_plain, ids)
        st.close()

    def test_err(self):
        rec, st, nm, _ = fakes.make_recorder()
        _, r_err, r_plain = seed(rec)
        res = search_mod.search(st, nm, "err:")
        ids = {h["run_id"] for h in res["hits"]}
        self.assertIn(r_err, ids)
        self.assertNotIn(r_plain, ids)
        res2 = search_mod.search(st, nm, "err:炸了")
        self.assertIn(r_err, {h["run_id"] for h in res2["hits"]})
        st.close()

    def test_user(self):
        rec, st, nm, _ = fakes.make_recorder()
        r_tool, _, _ = seed(rec)
        res = search_mod.search(st, nm, "user:掷个骰子")
        self.assertIn(r_tool, {h["run_id"] for h in res["hits"]})
        st.close()

    def test_free_word(self):
        rec, st, nm, _ = fakes.make_recorder()
        r_tool, _, _ = seed(rec)
        res = search_mod.search(st, nm, "掷出来是6")
        ids = {h["run_id"] for h in res["hits"]}
        self.assertIn(r_tool, ids)
        hit = next(h for h in res["hits"] if h["run_id"] == r_tool)
        self.assertTrue(hit["snippet"])
        st.close()

    def test_umo_filter(self):
        rec, st, nm, _ = fakes.make_recorder()
        seed(rec)
        res = search_mod.search(st, nm, "掷", umo="不存在的umo")
        self.assertEqual(res["hits"], [])
        st.close()

    def test_empty(self):
        rec, st, nm, _ = fakes.make_recorder()
        seed(rec)
        self.assertEqual(search_mod.search(st, nm, "  ")["hits"], [])
        st.close()

    def test_hits_contain_term(self):
        # S11：展示出来的命中事件要真含检索词
        rec, st, nm, _ = fakes.make_recorder()
        seed(rec)
        res = search_mod.search(st, nm, "掷出来是6")
        self.assertTrue(res["hits"])
        for h in res["hits"]:
            self.assertIn("掷出来是6", h["snippet"])
        st.close()

    def test_no_duplicate_hits(self):
        # S13：1 个运行里只有 1 个含词事件，就只出 1 条命中（修复 S11 时曾把同一事件追加两遍）
        import time
        rec, st, nm, _ = fakes.make_recorder()
        with st._w:
            st._w.execute("INSERT INTO runs(run_id,umo,source,status,sort_at,ord) VALUES('solo',?,'live','done',?,1)",
                          (fakes.UMO_WC, time.time()))
            st._w.execute("INSERT INTO events(run_id,seq,t,kind,name,text) VALUES('solo',1,?,'user',NULL,'掷骰子结果 7')",
                          (time.time(),))
        res = search_mod.search(st, nm, "掷骰子")
        self.assertEqual(len(res["hits"]), 1)
        st.close()

    def test_newest_first_beyond_one_chunk(self):
        # S14：命中运行数超过一个分块（500）后，仍要整体按时间从新到旧，不能只在块内排序
        import time
        rec, st, nm, _ = fakes.make_recorder()
        n, base = 1300, time.time() - 100 * 86400
        with st._w:
            st._w.executemany(
                "INSERT INTO runs(run_id,umo,source,status,sort_at,ord) VALUES(?,?,?,?,?,?)",
                [(f"r{i:05d}", fakes.UMO_WC, "live", "done", base + i, 1) for i in range(n)])
            st._w.executemany(
                "INSERT INTO events(run_id,seq,t,kind,name,text) VALUES(?,?,?,?,?,?)",
                [(f"r{i:05d}", 1, base + i, "user", None, f"掷骰子结果 {i}") for i in range(n)])
        res = search_mod.search(st, nm, "掷骰子", limit=50)
        ids = [h["run_id"] for h in res["hits"]]
        self.assertEqual(len(ids), 50)
        self.assertEqual(len(set(ids)), 50)  # 每个运行只有 1 个含词事件，不该重复
        self.assertEqual(ids, [f"r{i:05d}" for i in range(n - 1, n - 51, -1)])  # 最新的 50 个，从新到旧
        self.assertTrue(res["truncated"])
        st.close()

    def test_hits_only_fetch_needed_runs(self):
        # S15：命中运行很多时，只该取前面几十个运行的事件，不能把全部命中运行的事件都读一遍
        import time
        rec, st, nm, _ = fakes.make_recorder()
        n, base = 3000, time.time() - 100 * 86400
        with st._w:
            st._w.executemany(
                "INSERT INTO runs(run_id,umo,source,status,sort_at,ord) VALUES(?,?,?,?,?,?)",
                [(f"r{i:05d}", fakes.UMO_WC, "live", "done", base + i, 1) for i in range(n)])
            st._w.executemany(
                "INSERT INTO events(run_id,seq,t,kind,name,text) VALUES(?,?,?,?,?,?)",
                [(f"r{i:05d}", s, base + i, "user" if s == 0 else "text", None, f"掷骰子结果 {i} 第{s}段")
                 for i in range(n) for s in range(10)])
        conn = st._read_conn()
        fetched = []
        conn.set_trace_callback(lambda sql: fetched.append(sql))
        try:
            search_mod.search(st, nm, "掷骰子", limit=50)
        finally:
            conn.set_trace_callback(None)
        # 取事件正文的语句只该跑很少几次（原写法对 3000 个运行分 6 块各取一次，且每块都读完整事件）
        text_fetches = [s for s in fetched if "e.text" in s or "SELECT run_id, seq, kind" in s]
        self.assertLessEqual(len(text_fetches), 3, f"取事件正文的语句跑了 {len(text_fetches)} 次")
        st.close()

    def test_big_batch(self):
        # S7：4 万命中时 IN (?,…) 一次全塞会超 SQLite 变量上限
        import time
        rec, st, nm, _ = fakes.make_recorder()
        old = time.time() - 200 * 86400
        with st._w:
            st._w.executemany(
                "INSERT INTO runs(run_id,umo,source,status,sort_at,ord) VALUES(?,?,?,?,?,?)",
                [(f"big{i}", fakes.UMO_WC, "live", "done", old + i, 1) for i in range(40000)])
            st._w.executemany(
                "INSERT INTO events(run_id,seq,t,kind,name,text) VALUES(?,?,?,?,?,?)",
                [(f"big{i}", 2, old + i, "tool_call", "bigtool", "{}") for i in range(40000)])
        res = search_mod.search(st, nm, "tool:bigtool")
        self.assertTrue(res["hits"])
        self.assertEqual(st.purge(90), 40000)
        st.close()


if __name__ == "__main__":
    unittest.main()
