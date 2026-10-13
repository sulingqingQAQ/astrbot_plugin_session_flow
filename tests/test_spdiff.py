# -*- coding: utf-8 -*-
"""v0.1.9：system prompt 和上一轮对比（sf/spdiff.py + api/sysprompt_diff）。"""

from __future__ import annotations

import asyncio
import time
import unittest

import fakes
from sf import spdiff
from sf import store as store_mod
from sf import webapi as webapi_mod


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def rebuild(res, which):
    """用逐行结果还原文本：which="new" 还原这一轮，"old" 还原上一轮。能还原回去说明没丢行、没串行。"""
    out = []
    for it in res["lines"]:
        k = it["k"]
        if k == "same":
            out.append(it["t"])
        elif k == "chg":
            out.append("".join(t for kind, t in it["segs"] if (kind != 2 if which == "new" else kind != 1)))
        elif k == "add" and which == "new":
            out.append(it["t"])
        elif k == "del" and which == "old":
            out.append(it["t"])
    return "\n".join(out)


# =================================================================== 行内对比
class TestInline(unittest.TestCase):
    def test_only_changed_digits_are_boxed(self):
        segs = spdiff.inline("当前时间: 2026-10-13 07:23 周二", "当前时间: 2026-10-13 07:24 周二")
        self.assertIn((2, "23"), segs)
        self.assertIn((1, "24"), segs)
        self.assertEqual("".join(t for k, t in segs if k != 2), "当前时间: 2026-10-13 07:24 周二")
        self.assertEqual("".join(t for k, t in segs if k != 1), "当前时间: 2026-10-13 07:23 周二")

    def test_adjacent_changes_fold_into_one_box(self):
        # 23:51 → 24:03：中间的冒号并进去，框连成一块，不碎成三截
        segs = spdiff.inline("时间 07:23:51 结束", "时间 07:24:03 结束")
        self.assertIn((2, "23:51"), segs)
        self.assertIn((1, "24:03"), segs)

    def test_unchanged_line(self):
        self.assertEqual(spdiff.inline("一样", "一样"), [(0, "一样")])

    def test_dissimilar_lines_become_whole_replace(self):
        segs = spdiff.inline("完全不一样的一行文字内容", "xyz 123 abc!")
        self.assertEqual(segs, [(2, "完全不一样的一行文字内容"), (1, "xyz 123 abc!")])

    def test_cjk_chars_are_individual_tokens(self):
        segs = spdiff.inline("你是小苏，一个安静的女孩", "你是小苏，一个活泼的女孩")
        self.assertIn((2, "安静"), segs)
        self.assertIn((1, "活泼"), segs)

    def test_huge_line_does_not_hang(self):
        old = "词 " * (spdiff.MAX_TOKENS + 10)
        t0 = time.time()
        segs = spdiff.inline(old, old + "尾巴")
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual("".join(t for k, t in segs if k != 2), old + "尾巴")

    def test_long_line_with_one_number_changed(self):
        base = "背景设定：" + "安静而温柔，" * 300
        old, new = base + "当前 7 点" + base, base + "当前 8 点" + base
        segs = spdiff.inline(old, new)
        self.assertIn((2, "7"), segs)
        self.assertIn((1, "8"), segs)
        self.assertEqual(sum(len(t) for k, t in segs if k in (1, 2)), 2)


# =================================================================== 逐行对比
class TestBuild(unittest.TestCase):
    OLD = "你是小苏。\n当前时间: 07:23\n规则一\n规则二\n结尾"
    NEW = "你是小苏。\n当前时间: 07:24\n规则一\n规则二\n新增一行\n结尾"

    def test_identical(self):
        r = spdiff.build(self.OLD, self.OLD)
        self.assertTrue(r["identical"])
        self.assertEqual((r["n_hunks"], r["hunks"]), (0, []))

    def test_two_hunks_with_counts_and_line_numbers(self):
        r = spdiff.build(self.OLD, self.NEW)
        self.assertFalse(r["identical"])
        self.assertEqual(r["n_hunks"], 2)
        h1, h2 = r["hunks"]
        self.assertEqual((h1["n"], h1["old"], h1["new"]), (2, "当前时间: 07:23", "当前时间: 07:24"))
        self.assertEqual((h2["n"], h2["old_lines"], h2["new_lines"]), (5, 0, 1))
        self.assertEqual((h1["added"], h1["removed"]), (2, 2))          # 只算框住的那两个字
        self.assertEqual(r["added"], 2 + len("新增一行") + 1)
        self.assertNotIn("lines", r)                                    # 不要求就不带逐行结果

    def test_lines_rebuild_both_texts(self):
        r = spdiff.build(self.OLD, self.NEW, with_lines=True)
        self.assertEqual(rebuild(r, "new"), self.NEW)
        self.assertEqual(rebuild(r, "old"), self.OLD)
        kinds = [it["k"] for it in r["lines"]]
        self.assertEqual(kinds.count("chg"), 1)
        self.assertEqual(kinds.count("add"), 1)
        self.assertTrue(all("h" in it for it in r["lines"] if it["k"] != "same"))

    def test_pure_delete(self):
        r = spdiff.build("甲\n乙\n丙", "甲\n丙", with_lines=True)
        self.assertEqual(r["n_hunks"], 1)
        self.assertEqual((r["hunks"][0]["old_lines"], r["hunks"][0]["new_lines"]), (1, 0))
        self.assertEqual(rebuild(r, "new"), "甲\n丙")
        self.assertEqual(rebuild(r, "old"), "甲\n乙\n丙")

    def test_unequal_replace_pairs_similar_lines(self):
        old = "头\n每次回复不超过三句话\n不要说教\n尾"
        new = "头\n每次回复不超过五句话\n尾"
        r = spdiff.build(old, new, with_lines=True)
        self.assertEqual(rebuild(r, "new"), new)
        self.assertEqual(rebuild(r, "old"), old)
        chg = [it for it in r["lines"] if it["k"] == "chg"]
        self.assertEqual(len(chg), 1)                                  # 相似的那行配上了，只框“三”→“五”
        self.assertIn([2, "三"], chg[0]["segs"])
        self.assertTrue(any(it["k"] == "del" and it["t"] == "不要说教" for it in r["lines"]))

    def test_long_line_pairs_and_unrelated_changes_split_into_separate_hunks(self):
        # 一个替换块里：删掉一个块 + 新增一行 + 一行很长的只改了一个数字。三件事要各算一处，不能并成一处
        long_a = "背景设定：" + "安静而温柔，慢热，不强势。" * 60
        old = "\n".join(["头", "<g>", "规则一内容", "规则二内容", "</g>", long_a + "（热度 7 级）", "尾"])
        new = "\n".join(["头", "提醒：该喝水了", long_a + "（热度 8 级）", "尾"])
        cur = [inj("rem", "提醒：该喝水了", "p_rem")]
        prev = [inj("g", "<g>\n规则一内容\n规则二内容\n</g>", "p_g")]
        r = spdiff.build(old, new, cur, prev, with_lines=True)
        self.assertEqual(rebuild(r, "new"), new)
        self.assertEqual(rebuild(r, "old"), old)
        kinds = [(h["old_lines"], h["new_lines"]) for h in r["hunks"]]
        self.assertEqual(sorted(kinds), sorted([(0, 1), (4, 0), (1, 1)]))        # 只加 / 只删 / 配上的（来源各不相同，不并）
        by = {(h["old_lines"], h["new_lines"]): h["who"] for h in r["hunks"]}
        self.assertEqual([(w["plugin"], w["side"]) for w in by[(0, 1)]], [("p_rem", "new")])
        self.assertEqual([(w["plugin"], w["side"]) for w in by[(4, 0)]], [("p_g", "old")])
        self.assertEqual(by[(1, 1)], [{"attrib": "earlier", "side": "new"}])
        chg = next(it for it in r["lines"] if it["k"] == "chg")
        self.assertIn([2, "7"], chg["segs"])
        self.assertIn([1, "8"], chg["segs"])                                      # 长行只框数字，不是整行
        self.assertLess(r["added"], 40)

    def test_same_source_neighbours_merge_into_one_hunk(self):
        # 记忆块里改了一行又追加一行：同一个插件带来的，算一处
        old = "头\n<memory>\n记忆B：喜欢战争雷霆\n</memory>\n尾"
        new = "头\n<memory>\n记忆B：喜欢战争雷霆和环世界\n记忆C：昼夜颠倒\n</memory>\n尾"
        cur = [inj("memory", "<memory>\n记忆B：喜欢战争雷霆和环世界\n记忆C：昼夜颠倒\n</memory>", "plugin_mem")]
        r = spdiff.build(old, new, cur, [], with_lines=True)
        self.assertEqual(r["n_hunks"], 1)
        self.assertEqual([(w["plugin"], w["side"]) for w in r["hunks"][0]["who"]], [("plugin_mem", "new")])
        self.assertEqual((r["hunks"][0]["old_lines"], r["hunks"][0]["new_lines"]), (1, 2))
        self.assertEqual(rebuild(r, "new"), new)
        self.assertEqual(rebuild(r, "old"), old)

    def test_hunk_numbers_and_lines_agree(self):
        r = spdiff.build("头\n甲\n乙\n尾", "头\n丙\n尾", with_lines=True)
        ids = {h["id"] for h in r["hunks"]}
        self.assertEqual({it["h"] for it in r["lines"] if it["k"] != "same"}, ids)
        self.assertEqual([h["id"] for h in r["hunks"]], list(range(1, len(r["hunks"]) + 1)))

    def test_crlf_only_difference(self):
        r = spdiff.build("甲\r\n乙", "甲\n乙")
        self.assertFalse(r["identical"])
        self.assertEqual(r["n_hunks"], 0)
        self.assertIn("换行", r["note"])

    def test_too_big_is_not_compared(self):
        r = spdiff.build("a" * (spdiff.MAX_CHARS + 1), "b")
        self.assertTrue(r["too_big"])
        self.assertEqual(r["hunks"], [])

    def test_5000_lines_one_change_is_fast(self):
        old = "\n".join(f"第 {i} 条规则：别说废话" for i in range(5000))
        new = old.replace("第 2500 条规则", "第 2500 号规则")
        t0 = time.time()
        r = spdiff.build(old, new, with_lines=True)
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(r["n_hunks"], 1)
        self.assertEqual(rebuild(r, "new"), new)

    def test_many_blank_lines_do_not_blow_up(self):
        old = "\n".join(["", "x"] * 1500)
        new = "\n".join(["", "x"] * 1499 + ["", "y"])
        t0 = time.time()
        r = spdiff.build(old, new)
        self.assertLess(time.time() - t0, 5.0)
        self.assertEqual(r["n_hunks"], 1)

    def test_hunk_cap_still_returns_lines(self):
        old = "\n".join(f"行{i}" for i in range(0, 1200, 1))
        new = "\n".join(f"改{i}" if i % 2 == 0 else f"行{i}" for i in range(0, 1200, 1))
        r = spdiff.build(old, new, with_lines=True)
        self.assertGreater(r["n_hunks"], spdiff.MAX_HUNKS)
        self.assertTrue(r["truncated"])
        self.assertEqual(rebuild(r, "new"), new)


# =================================================================== 谁带来的
def inj(name, text, plugin=None, handler="on_llm_request", attrib="exact", guess=None):
    d = {"name": name, "text": text, "attrib": attrib}
    if plugin:
        d["plugin"], d["handler"] = plugin, handler
    if guess:
        d["guess"] = guess
    return d


class TestAttribute(unittest.TestCase):
    def test_matches_inject_block_containing_the_line(self):
        got = spdiff.attribute(["当前时间: 07:24"], [inj("clock", "<t>\n当前时间: 07:24\n</t>", "plugin_clock"),
                                                    inj("other", "毫无关系的内容", "plugin_x")])
        self.assertEqual([g["plugin"] for g in got], ["plugin_clock"])
        self.assertEqual(got[0]["handler"], "on_llm_request")

    def test_no_block_contains_it(self):
        self.assertEqual(spdiff.attribute(["人设改了"], [inj("a", "别的内容", "p")]), [])

    def test_blank_or_separator_lines_have_nothing_to_match(self):
        self.assertIsNone(spdiff.attribute(["", "---", "  "], [inj("a", "---", "p")]))

    def test_generic_line_does_not_steal_attribution(self):
        # 三行里两行是 A 的，只有一行恰好 B 里也有：B 命中数不到最多的 3 成，不算
        lines = ["甲甲甲甲", "乙乙乙乙", "丙丙丙丙", "丁丁丁丁"]
        got = spdiff.attribute(lines, [inj("A", "\n".join(lines), "pA"), inj("B", "丙丙丙丙", "pB")])
        self.assertEqual([g["plugin"] for g in got], ["pA"])

    def test_unattributed_keeps_guess(self):
        got = spdiff.attribute(["系统提醒内容"], [inj("sys", "系统提醒内容", attrib="unattributed", guess=["plugin_g"])])
        self.assertEqual((got[0]["attrib"], got[0]["guess"], "plugin" in got[0]), ("unattributed", ["plugin_g"], False))


class TestWho(unittest.TestCase):
    CUR = [inj("clock", "当前时间: 07:24", "plugin_clock"), inj("rule", "群聊规则：别刷屏", "plugin_lingxi")]
    PREV = [inj("clock", "当前时间: 07:23", "plugin_clock"), inj("rule", "群聊规则：别刷屏", "plugin_lingxi")]

    def test_new_side_gets_plugin(self):
        r = spdiff.build("头\n当前时间: 07:23", "头\n当前时间: 07:24", self.CUR, self.PREV)
        w = r["hunks"][0]["who"]
        self.assertEqual([(x["plugin"], x["side"]) for x in w], [("plugin_clock", "new")])
        self.assertEqual(r["who_summary"][0]["plugin"], "plugin_clock")

    def test_deleted_lines_use_previous_run_injects(self):
        r = spdiff.build("头\n群聊规则：别刷屏\n尾", "头\n尾", self.CUR, self.PREV)
        w = r["hunks"][0]["who"]
        self.assertEqual([(x["plugin"], x["side"]) for x in w], [("plugin_lingxi", "old")])

    def test_unmatched_change_is_earlier(self):
        r = spdiff.build("头\n人设甲", "头\n人设乙", self.CUR, self.PREV)
        self.assertEqual(r["hunks"][0]["who"], [{"attrib": "earlier", "side": "new"}])

    def test_replace_adds_old_side_when_it_was_another_plugin(self):
        cur = [inj("rule", "群聊规则：别刷屏", "plugin_lingxi")]
        prev = [inj("rule", "群聊规则：随便聊", "plugin_old")]
        r = spdiff.build("头\n群聊规则：随便聊", "头\n群聊规则：别刷屏", cur, prev)
        w = r["hunks"][0]["who"]
        self.assertEqual([(x["plugin"], x["side"]) for x in w], [("plugin_lingxi", "new"), ("plugin_old", "old")])

    def test_blank_line_change_has_no_who(self):
        r = spdiff.build("头\n尾", "头\n\n尾", self.CUR, self.PREV)
        self.assertEqual(r["hunks"][0]["who"], [])

    def test_who_list_is_capped(self):
        many = [inj(f"b{i}", f"共同句子 第{i}段内容", f"p{i}") for i in range(8)]
        line = "\n".join(f"共同句子 第{i}段内容" for i in range(8))
        r = spdiff.build("头", "头\n" + line, many, [])
        w = r["hunks"][0]["who"]
        self.assertEqual(len(w), spdiff.MAX_WHO + 1)
        self.assertEqual(w[-1]["attrib"], "more")

    def test_summary_counts_hunks_per_source(self):
        old = "头\n当前时间: 07:23\n中\n当前时间: 07:23 再来\n尾"
        new = "头\n当前时间: 07:24\n中\n当前时间: 07:24 再来\n尾"
        cur = [inj("clock", "当前时间: 07:24\n当前时间: 07:24 再来", "plugin_clock")]
        r = spdiff.build(old, new, cur, [])
        self.assertEqual(r["n_hunks"], 2)
        self.assertEqual([(s["plugin"], s["n"]) for s in r["who_summary"]], [("plugin_clock", 2)])


# =================================================================== 接口
UMO = fakes.UMO_QQ
SP1 = "你是小苏。\n当前时间: 07:23\n记忆：猫叫希尔薇"
SP2 = "你是小苏。\n当前时间: 07:24\n记忆：猫叫希尔薇\n群聊规则：别刷屏"
SP3 = "你是小苏。\n当前时间: 07:24\n记忆：猫叫希尔薇"


def put_run(st, rid, i, text, injects, umo=UMO):
    base = time.time() - 3600 + i * 60
    h = None
    if text is not None:
        h = f"{i:02d}" * 32
        st.put_blob(h, text)
    st.insert_run({"run_id": rid, "umo": umo, "cid": "c", "source": "live", "status": "done",
                   "sort_at": base, "ord": 1, "received_at": base, "begin_at": base + 1, "end_at": base + 5,
                   "sender_id": "1", "sender_name": "甲", "provider_id": "p", "model": "m", "user_text": f"问{i}",
                   "reply_text": f"答{i}", "sysprompt_hash": h, "contexts_len": 2, "n_steps": 1, "n_tools": 0})
    evs = [{"run_id": rid, "seq": 1, "t": base, "kind": "user", "text": f"问{i}"}]
    for n, (name, t, plugin, attrib) in enumerate(injects, start=2):
        data = {"source": "system_prompt", "attrib": attrib}
        if plugin:
            data.update(plugin=plugin, handler="on_llm_request")
        evs.append({"run_id": rid, "seq": n, "t": base, "kind": "inject", "step": 0, "name": name, "text": t, "data": data})
    st.insert_events(evs)


def make(guesser=None):
    st = store_mod.Store(fakes.tmp_db_path())
    put_run(st, "d0#1", 0, SP1, [("clock", "当前时间: 07:23", "plugin_clock", "exact")])
    put_run(st, "d1#1", 1, SP2, [("clock", "当前时间: 07:24", "plugin_clock", "exact"),
                                 ("rule", "群聊规则：别刷屏", "plugin_lingxi", "exact")])
    put_run(st, "d2#1", 2, SP3, [("clock", "当前时间: 07:24", "plugin_clock", "exact")])
    api = webapi_mod.SessionFlowWebApi(st, None)
    api.guesser = guesser
    return st, api


class TestApi(unittest.TestCase):
    def test_route_registered(self):
        names = {r for r, _, _ in webapi_mod.SessionFlowWebApi(None, None).routes()}
        self.assertIn("api/sysprompt_diff", names)

    def test_bad_args(self):
        st, api = make()
        self.assertEqual(run(api.handle_sysprompt_diff({}, {}))["status"], "error")
        self.assertEqual(run(api.handle_sysprompt_diff({"id": "nope"}, {}))["status"], "error")
        st.close()

    def test_first_run_has_nothing_to_compare(self):
        st, api = make()
        d = run(api.handle_sysprompt_diff({"id": "d0#1"}, {}))["data"]
        self.assertEqual(d["state"], "no_prev")
        self.assertIn("第一轮", d["text"])
        st.close()

    def test_changed_with_plugin_names(self):
        st, api = make()
        d = run(api.handle_sysprompt_diff({"id": "d1#1"}, {}))["data"]
        self.assertEqual(d["state"], "changed")
        self.assertEqual(d["prev"]["run_id"], "d0#1")
        self.assertEqual(d["n_hunks"], 2)
        who = {h["id"]: [(w["plugin"], w["side"]) for w in h["who"]] for h in d["hunks"]}
        self.assertEqual(who, {1: [("plugin_clock", "new")], 2: [("plugin_lingxi", "new")]})
        self.assertNotIn("lines", d)
        self.assertEqual((d["old_size"], d["new_size"]), (len(SP1.encode()), len(SP2.encode())))
        st.close()

    def test_lines_only_when_asked(self):
        st, api = make()
        d = run(api.handle_sysprompt_diff({"id": "d1#1", "lines": "1"}, {}))["data"]
        self.assertEqual(rebuild(d, "new"), SP2)
        st.close()

    def test_deleted_block_is_credited_to_previous_run_plugin(self):
        st, api = make()
        d = run(api.handle_sysprompt_diff({"id": "d2#1"}, {}))["data"]
        self.assertEqual(d["n_hunks"], 1)
        self.assertEqual([(w["plugin"], w["side"]) for w in d["hunks"][0]["who"]], [("plugin_lingxi", "old")])
        st.close()

    def test_same_prompt(self):
        st, api = make()
        put_run(st, "d3#1", 3, SP3, [])
        d = run(api.handle_sysprompt_diff({"id": "d3#1"}, {}))["data"]
        self.assertEqual(d["state"], "same")
        self.assertEqual(d["prev"]["run_id"], "d2#1")
        st.close()

    def test_run_without_prompt(self):
        st, api = make()
        put_run(st, "d3#1", 3, None, [])
        d = run(api.handle_sysprompt_diff({"id": "d3#1"}, {}))["data"]
        self.assertEqual(d["state"], "no_prompt")
        st.close()

    def test_previous_run_without_prompt_falls_back_to_earlier_one(self):
        st, api = make()
        put_run(st, "d3#1", 3, None, [])                       # 中间这轮没存 system prompt
        put_run(st, "d4#1", 4, SP1, [("clock", "当前时间: 07:23", "plugin_clock", "exact")])
        d = run(api.handle_sysprompt_diff({"id": "d4#1"}, {}))["data"]
        self.assertEqual(d["prev"]["run_id"], "d2#1")           # 越过没存的那一轮
        self.assertEqual(d["prev"]["skipped"], 1)
        self.assertEqual(d["state"], "changed")
        st.close()

    def test_all_earlier_runs_without_prompt(self):
        st = store_mod.Store(fakes.tmp_db_path())
        put_run(st, "e0#1", 0, None, [])
        put_run(st, "e1#1", 1, SP1, [])
        api = webapi_mod.SessionFlowWebApi(st, None)
        d = run(api.handle_sysprompt_diff({"id": "e1#1"}, {}))["data"]
        self.assertEqual(d["state"], "no_prev")
        self.assertIn("没存", d["text"])
        st.close()

    def test_other_session_is_not_the_previous_run(self):
        st, api = make()
        put_run(st, "x0#1", 5, SP1, [], umo="aiocqhttp:GroupMessage:999")
        d = run(api.handle_sysprompt_diff({"id": "x0#1"}, {}))["data"]
        self.assertEqual(d["state"], "no_prev")
        st.close()

    def test_unattributed_block_gets_guess(self):
        class G:
            def guess(self, name):
                return ["plugin_g"] if name == "reminder" else []

        st = store_mod.Store(fakes.tmp_db_path())
        put_run(st, "g0#1", 0, "头", [])
        put_run(st, "g1#1", 1, "头\n系统提醒：喝水", [("reminder", "系统提醒：喝水", None, "unattributed")])
        api = webapi_mod.SessionFlowWebApi(st, None)
        api.guesser = G()
        d = run(api.handle_sysprompt_diff({"id": "g1#1"}, {}))["data"]
        w = d["hunks"][0]["who"][0]
        self.assertEqual((w["attrib"], w["guess"], "plugin" in w), ("unattributed", ["plugin_g"], False))
        st.close()

    def test_run_detail_payload_unchanged(self):
        # 不能影响单轮详情和导出的数据形状
        st, api = make()
        d = run(api.handle_run({"id": "d1#1"}, {}))["data"]
        self.assertEqual(set(d), {"run", "events", "sysprompt", "context", "timing"})
        self.assertEqual(set(d["sysprompt"]), {"hash", "size", "same_as_prev"})
        st.close()


if __name__ == "__main__":
    unittest.main()
