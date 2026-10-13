# -*- coding: utf-8 -*-
"""v0.1.4：回合列表按页码取（新的在前）+ 会话详情里“搜本对话”（精确限定会话、内容类型、分页）。"""

from __future__ import annotations

import asyncio
import time
import unittest

import fakes
from sf import search as search_mod
from sf import store as store_mod
from sf import webapi as webapi_mod

UMO_A = fakes.UMO_WC
UMO_B = fakes.UMO_QQ
# 比 UMO_A 多一截：“包含匹配”会把它也当成 UMO_A 命中，精确匹配不会
UMO_A_LONGER = UMO_A + "-2"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def make_store():
    return store_mod.Store(fakes.tmp_db_path())


def add_run(st, rid, umo, i, user="你好", reply="好", events=(), **kw):
    f = {"run_id": rid, "umo": umo, "cid": "c", "source": "live", "status": "done",
         "sort_at": 1790000000.0 + i, "ord": 0, "user_text": user, "reply_text": reply}
    f.update(kw)
    st.insert_run(f)
    evs = [{"run_id": rid, "seq": 1, "kind": "user", "text": user},
           {"run_id": rid, "seq": 2, "kind": "text", "text": reply}]
    for j, e in enumerate(events):
        evs.append({"run_id": rid, "seq": 3 + j, **e})
    st.insert_events(evs)


def seed_many(st, n=45):
    for i in range(n):
        add_run(st, f"a{i:03d}", UMO_A, i, user=f"问题{i}", reply=f"回答{i}")
    for i in range(5):
        add_run(st, f"b{i:03d}", UMO_B, 100 + i, user=f"别处{i}")


class TestStorePaging(unittest.TestCase):
    def test_count_and_offset_newest_first(self):
        st = make_store()
        seed_many(st)
        self.assertEqual(st.count_runs(UMO_A), 45)
        self.assertEqual(st.count_runs(UMO_B), 5)
        p1 = st.list_runs_offset(UMO_A, offset=0, limit=20)
        p2 = st.list_runs_offset(UMO_A, offset=20, limit=20)
        p3 = st.list_runs_offset(UMO_A, offset=40, limit=20)
        self.assertEqual([len(p1), len(p2), len(p3)], [20, 20, 5])
        self.assertEqual(p1[0]["run_id"], "a044")           # 第 1 页最新在最上
        self.assertEqual(p3[-1]["run_id"], "a000")          # 最后一页最旧在最下
        allids = [r["run_id"] for r in p1 + p2 + p3]
        self.assertEqual(len(set(allids)), 45)              # 不重不漏
        self.assertEqual(allids, sorted(allids, reverse=True))
        st.close()

    def test_same_second_stable(self):
        # 同一秒多轮：翻页不丢不重（rowid 决胜）
        st = make_store()
        for i in range(7):
            add_run(st, f"t{i}", UMO_A, 0)
        seen = []
        for off in (0, 3, 6):
            seen += [r["run_id"] for r in st.list_runs_offset(UMO_A, offset=off, limit=3)]
        self.assertEqual(sorted(seen), [f"t{i}" for i in range(7)])
        st.close()

    def test_runs_by_ids_keeps_order_and_skips_missing(self):
        st = make_store()
        seed_many(st, 5)
        got = st.runs_by_ids(["a003", "nope", "a001", "a004"])
        self.assertEqual([r["run_id"] for r in got], ["a003", "a001", "a004"])
        self.assertEqual(got[0]["user_text"], "问题3")
        self.assertEqual(st.runs_by_ids([]), [])
        st.close()

    def test_runs_by_ids_more_than_one_chunk(self):
        st = make_store()
        for i in range(620):
            add_run(st, f"x{i:04d}", UMO_A, i)
        ids = [f"x{i:04d}" for i in range(619, -1, -1)]
        got = st.runs_by_ids(ids)
        self.assertEqual([r["run_id"] for r in got], ids)
        st.close()


class TestSearchScoped(unittest.TestCase):
    def test_exact_umo_not_prefix(self):
        st = make_store()
        add_run(st, "r1", UMO_A, 1, user="骰子来一个")
        add_run(st, "r2", UMO_A_LONGER, 2, user="骰子来一个")
        res = search_mod.search_runs_page(st, "骰子", umo=UMO_A)
        self.assertEqual(res["ids"], ["r1"])
        self.assertEqual(res["total"], 1)
        # 旧的整库检索：umo 当片段是“包含匹配”，行为不变（两个都命中）
        old = search_mod.search(st, fakes_names(), "骰子", umo=UMO_A)
        self.assertEqual({h["run_id"] for h in old["hits"]}, {"r1", "r2"})
        st.close()

    def test_other_umo_never_leaks(self):
        st = make_store()
        seed_many(st)
        res = search_mod.search_runs_page(st, "别处", umo=UMO_A)
        self.assertEqual(res["total"], 0)
        res = search_mod.search_runs_page(st, "别处", umo=UMO_B)
        self.assertEqual(res["total"], 5)
        st.close()

    def test_paging_newest_first_and_clamp(self):
        st = make_store()
        seed_many(st)
        r1 = search_mod.search_runs_page(st, "问题", umo=UMO_A, page=1, size=20)
        self.assertEqual((r1["total"], r1["pages"], r1["page"]), (45, 3, 1))
        self.assertEqual(r1["ids"][0], "a044")
        r3 = search_mod.search_runs_page(st, "问题", umo=UMO_A, page=3, size=20)
        self.assertEqual(len(r3["ids"]), 5)
        r9 = search_mod.search_runs_page(st, "问题", umo=UMO_A, page=9, size=20)
        self.assertEqual(r9["page"], 3)                      # 超页收到最后一页
        self.assertEqual(r9["ids"], r3["ids"])
        st.close()

    def test_short_word_like_path(self):
        st = make_store()
        add_run(st, "r1", UMO_A, 1, user="早安", reply="早")
        add_run(st, "r2", UMO_A, 2, user="晚安", reply="晚")
        res = search_mod.search_runs_page(st, "早安", umo=UMO_A)   # 2 字走 LIKE
        self.assertEqual(res["ids"], ["r1"])
        st.close()

    def test_hits_have_seq_snippet_and_cap(self):
        st = make_store()
        events = [{"kind": "tool_result", "name": "dice", "text": f"第{k}次含关键词龙虾沙拉"} for k in range(5)]
        add_run(st, "r1", UMO_A, 1, user="随便", events=events)
        res = search_mod.search_runs_page(st, "龙虾沙拉", umo=UMO_A)
        hits = res["hits"]["r1"]
        self.assertEqual(len(hits), 3)                       # 每回合最多展示 3 条
        self.assertEqual([h["seq"] for h in hits], sorted(h["seq"] for h in hits))
        self.assertTrue(all("龙虾沙拉" in h["snippet"] for h in hits))
        self.assertIn("r1", res["more"])                     # 还有没展示的命中
        st.close()

    def test_snippet_follows_the_word_that_is_in_the_text(self):
        st = make_store()
        long_prefix = "无关的话" * 30
        add_run(st, "r1", UMO_A, 1, user="先说苹果", events=[
            {"kind": "text", "text": long_prefix + "最后才出现香蕉"}])
        res = search_mod.search_runs_page(st, "苹果 香蕉", umo=UMO_A)
        hit_texts = [h["snippet"] for h in res["hits"]["r1"]]
        self.assertTrue(any("香蕉" in s for s in hit_texts))
        self.assertTrue(any("苹果" in s for s in hit_texts))
        st.close()

    def test_kind_filter(self):
        st = make_store()
        add_run(st, "r1", UMO_A, 1, user="我喊了关键字", reply="好的",
                events=[{"kind": "tool_call", "name": "dice", "text": "{}"},
                        {"kind": "tool_result", "name": "dice", "text": "6"}])
        add_run(st, "r2", UMO_A, 2, user="随便", reply="回复里有关键字",
                events=[{"kind": "inject", "name": "lore", "text": "注入里有关键字"},
                        {"kind": "think", "text": "想了想关键字"}])
        add_run(st, "r3", UMO_A, 3, user="出错了", reply="炸",
                events=[{"kind": "tool_result", "name": "bad", "text": "error", "is_error": 1}])
        f = lambda q, k: search_mod.search_runs_page(st, q, umo=UMO_A, kind=k)["ids"]  # noqa: E731
        self.assertEqual(sorted(f("关键字", "")), ["r1", "r2"])
        self.assertEqual(f("关键字", "user"), ["r1"])
        self.assertEqual(f("关键字", "text"), ["r2"])
        self.assertEqual(f("关键字", "inject"), ["r2"])
        self.assertEqual(f("关键字", "think"), ["r2"])
        self.assertEqual(f("关键字", "tool"), [])
        # 只选类型、不写词：有这一类事件的回合
        self.assertEqual(f("", "tool"), ["r3", "r1"])
        self.assertEqual(f("", "error"), ["r3"])
        self.assertEqual(f("", "inject"), ["r2"])
        # 命中事件也只挑这一类
        res = search_mod.search_runs_page(st, "关键字", umo=UMO_A, kind="inject")
        self.assertEqual({h["kind"] for h in res["hits"]["r2"]}, {"inject"})
        st.close()

    def test_unknown_kind_is_no_filter_and_empty_query_is_empty(self):
        st = make_store()
        seed_many(st, 3)
        self.assertEqual(search_mod.clean_kind("乱写"), "")
        res = search_mod.search_runs_page(st, "问题", umo=UMO_A, kind="乱写")
        self.assertEqual(res["total"], 3)
        res = search_mod.search_runs_page(st, "", umo=UMO_A)
        self.assertEqual((res["total"], res["ids"]), (0, []))
        st.close()

    def test_structured_tokens_inside_scope(self):
        st = make_store()
        add_run(st, "r1", UMO_A, 1, events=[{"kind": "tool_call", "name": "dice", "text": "{}"}])
        add_run(st, "r2", UMO_B, 2, events=[{"kind": "tool_call", "name": "dice", "text": "{}"}])
        res = search_mod.search_runs_page(st, "tool:dice", umo=UMO_A)
        self.assertEqual(res["ids"], ["r1"])
        add_run(st, "r3", UMO_A, 3, user="a_b%c")
        res = search_mod.search_runs_page(st, "user:a_b%c", umo=UMO_A)    # 通配符按字面量
        self.assertEqual(res["ids"], ["r3"])
        st.close()


def fakes_names():
    from sf import names as names_mod
    return names_mod.NameResolver(None)


class TestHandleRunsPaged(unittest.TestCase):
    def make(self):
        st = make_store()
        seed_many(st)
        st.touch_umo(UMO_A, "webchat", "FriendMessage", "测试用户")
        st.recompute_umo(UMO_A)
        return st, webapi_mod.SessionFlowWebApi(st, fakes_names())

    def test_default_page_shape(self):
        st, api = self.make()
        res = run(api.handle_runs({"umo": UMO_A, "page": "1", "size": "20"}, {}))
        self.assertEqual(res["status"], "ok")
        d = res["data"]
        self.assertEqual((d["total"], d["pages"], d["page"], d["size"], d["searching"]), (45, 3, 1, 20, False))
        self.assertEqual(len(d["runs"]), 20)
        self.assertEqual(d["runs"][0]["run_id"], "a044")
        self.assertIn("tools_used", d["runs"][0])
        self.assertEqual(d["umo"]["umo"], UMO_A)
        st.close()

    def test_sizes_and_clamp(self):
        st, api = self.make()
        d = run(api.handle_runs({"umo": UMO_A, "page": "1", "size": "50"}, {}))["data"]
        self.assertEqual((len(d["runs"]), d["pages"]), (45, 1))
        d = run(api.handle_runs({"umo": UMO_A, "page": "99", "size": "20"}, {}))["data"]
        self.assertEqual((d["page"], len(d["runs"])), (3, 5))
        d = run(api.handle_runs({"umo": UMO_A, "page": "-4", "size": "abc"}, {}))["data"]
        self.assertEqual((d["page"], d["size"]), (1, 20))
        st.close()

    def test_empty_conversation(self):
        st, api = self.make()
        d = run(api.handle_runs({"umo": "nobody:FriendMessage:x", "page": "1"}, {}))["data"]
        self.assertEqual((d["total"], d["pages"], d["page"], d["runs"]), (0, 1, 1, []))
        st.close()

    def test_search_mode_has_hits(self):
        st, api = self.make()
        d = run(api.handle_runs({"umo": UMO_A, "page": "1", "size": "20", "q": "问题4"}, {}))["data"]
        self.assertTrue(d["searching"])
        self.assertEqual(d["total"], 6)  # 问题4、问题40…44 里包含“问题4”：4,40,41,42,43,44
        self.assertEqual(d["runs"][0]["run_id"], "a044")
        self.assertTrue(d["runs"][0]["hits"])
        self.assertIn("hits_more", d["runs"][0])
        st.close()

    def test_kind_only(self):
        st, api = self.make()
        d = run(api.handle_runs({"umo": UMO_A, "page": "1", "kind": "think"}, {}))["data"]
        self.assertTrue(d["searching"])
        self.assertEqual(d["total"], 0)
        st.close()

    def test_cursor_mode_still_works(self):
        # 老接口（limit + before 游标）不变
        st, api = self.make()
        d = run(api.handle_runs({"umo": UMO_A, "limit": "10"}, {}))["data"]
        self.assertEqual(len(d["runs"]), 10)
        self.assertTrue(d["has_more"])
        self.assertIn("next_before", d)
        self.assertNotIn("total", d)
        st.close()


if __name__ == "__main__":
    unittest.main()
