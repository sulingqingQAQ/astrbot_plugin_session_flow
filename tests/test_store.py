# -*- coding: utf-8 -*-
"""store 测试：建表幂等、blob 去重、FTS 中英文、清理、分页。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import fakes
from sf import store as store_mod


def make_store():
    return store_mod.Store(fakes.tmp_db_path()), None


def insert_run(st, run_id="r1", umo=None, **kw):
    f = {"run_id": run_id, "umo": umo or fakes.UMO_WC, "cid": "cid-1",
         "source": "live", "status": "done", "sort_at": time.time(), "ord": 0,
         "user_text": "你好", "reply_text": "好"}
    f.update(kw)
    st.insert_run(f)
    return run_id


class TestStore(unittest.TestCase):
    def test_schema_idempotent(self):
        st, _ = make_store()
        st2 = store_mod.Store(st.path)  # 同一文件再开一次不报错
        st2.close()
        st.close()

    def test_blob_dedup(self):
        import hashlib
        st, _ = make_store()
        h = hashlib.sha256("你是助手".encode("utf-8")).hexdigest()
        n1 = st.put_blob(h, "你是助手")
        n2 = st.put_blob(h, "你是助手")  # 同一哈希只存一次
        self.assertEqual(n1, n2)
        self.assertEqual(st.blob_hashes(), {h})
        blob = st.get_blob(h)
        self.assertEqual(blob["text"], "你是助手")
        self.assertEqual(blob["size"], len("你是助手".encode("utf-8")))
        st.close()

    def test_fts_chinese_long_and_short(self):
        st, _ = make_store()
        rid = insert_run(st)
        st.insert_events([
            {"run_id": rid, "seq": 1, "kind": "text", "text": "掷出来是八点很幸运"},
            {"run_id": rid, "seq": 2, "kind": "text", "text": "AB"},
        ])
        conn = st._read_conn()
        if st.fts:
            rows = conn.execute(
                "SELECT e.run_id FROM events_fts f JOIN events e ON e.id=f.rowid "
                "WHERE events_fts MATCH ?", ('"很幸运"',)).fetchall()
            self.assertTrue(rows, "3 字中文应走 FTS 命中")
        rows = conn.execute("SELECT DISTINCT run_id FROM events WHERE text LIKE '%AB%'").fetchall()
        self.assertTrue(rows, "2 字走 LIKE 也能命中")
        st.close()

    def test_fts_delete_sync(self):
        st, _ = make_store()
        rid = insert_run(st, sort_at=time.time() - 100 * 86400)
        st.insert_events([{"run_id": rid, "seq": 1, "kind": "text", "text": "待删除的独特词XYZ"}])
        n = st.purge(90)
        self.assertEqual(n, 1)
        conn = st._read_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"], 0)
        st.close()

    def test_touch_umo_recompute(self):
        st, _ = make_store()
        insert_run(st, "r1", user_text="第一句", reply_text="回一",
                   token_in=10, token_out=5, n_tools=2, status="done")
        insert_run(st, "r2", user_text="第二句", reply_text="回二",
                   token_in=7, token_out=3, n_tools=1, status="error", n_errors=1,
                   sort_at=time.time() + 1, ord=1)
        st.touch_umo(fakes.UMO_WC, "webchat", "FriendMessage", "测试用户")
        st.recompute_umo(fakes.UMO_WC)
        umos = st.list_umos()
        self.assertEqual(len(umos), 1)
        u = umos[0]
        self.assertEqual(u["run_count"], 2)
        self.assertEqual(u["tool_count"], 3)
        self.assertEqual(u["error_count"], 1)
        self.assertEqual(u["token_in"], 17)
        self.assertEqual(u["last_user"], "第二句")
        st.close()

    def test_list_runs_page(self):
        st, _ = make_store()
        base = time.time()
        for i in range(5):
            insert_run(st, f"r{i}", sort_at=base + i, ord=i)
        page, has_more, _ = st.list_runs_page(fakes.UMO_WC, limit=2)
        self.assertTrue(has_more)
        self.assertEqual([r["run_id"] for r in page], ["r3", "r4"])  # 升序最新两条
        before = (page[0]["sort_at"], page[0]["ord"])
        page2, has_more2, _ = st.list_runs_page(fakes.UMO_WC, before=before, limit=10)
        self.assertFalse(has_more2)
        self.assertEqual([r["run_id"] for r in page2], ["r0", "r1", "r2"])
        st.close()

    def test_same_second_paging_no_loss(self):
        # S8：同秒多轮 (sort_at, ord) 撞车，rowid 决胜，翻页不丢行
        st, _ = make_store()
        base = 1790000000.0
        for i in range(5):
            insert_run(st, f"tie{i}", sort_at=base, ord=1, user_text=f"q{i}")
        seen: list[str] = []
        before = None
        for _ in range(3):
            page, more, nxt = st.list_runs_page(fakes.UMO_WC, before=before, limit=2)
            seen += [r["run_id"] for r in page]
            if not more:
                break
            s, o, r = nxt.split(":")
            before = (float(s), int(o), int(r))
        self.assertEqual(sorted(seen), [f"tie{i}" for i in range(5)])
        st.close()

    def test_list_runs_page_has_text_preview(self):
        # 线上抓到的 bug：列表页没取正文，全显示“空提问”
        st, _ = make_store()
        insert_run(st, "r1", user_text="第一句提问", reply_text="回一",
                   error_text="炸了", sort_at=time.time(), ord=0)
        page, _, _ = st.list_runs_page(fakes.UMO_WC, limit=10)
        self.assertEqual(page[0]["user_text"], "第一句提问")
        self.assertEqual(page[0]["reply_text"], "回一")
        self.assertEqual(page[0]["error_text"], "炸了")
        insert_run(st, "r2", user_text="x" * 500, sort_at=time.time() + 1, ord=1)
        page, _, _ = st.list_runs_page(fakes.UMO_WC, limit=10)
        self.assertEqual(len(page[1]["user_text"]), 300)
        st.close()

    def test_tools_used_uses_run_index(self):
        # S12：tools_used 查询必须走 (run_id, kind, name)，不能扫 kind 全表
        st, _ = make_store()
        plan = " ".join(r[3] for r in st._read_conn().execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT name FROM events "
            "WHERE run_id=? AND kind='tool_call'", ("x",)))
        self.assertIn("ix_events_run_kind_name", plan)
        st.close()


if __name__ == "__main__":
    unittest.main()
