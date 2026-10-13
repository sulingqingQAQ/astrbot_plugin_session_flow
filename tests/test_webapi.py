# -*- coding: utf-8 -*-
"""webapi.SessionFlowWebApi 离线用例：handle_xxx 直接调（不依赖 quart）。"""

from __future__ import annotations

import asyncio
import unittest

import fakes
from sf import legacy as legacy_mod
from sf import webapi as webapi_mod

CALL = "call_1"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def seed():
    rec, st, nm, _ = fakes.make_recorder()
    rid, _ = fakes.drive_run(
        rec, span="w1", prompt="掷个骰子",
        tool_uses=[("dice", '{"sides": 6}', "6面")],
        new_messages=[
            fakes.assistant_msg("", tool_calls=[fakes.tool_call_msg(CALL, "dice", '{"sides": 6}')]),
            fakes.tool_msg("6面", CALL),
            fakes.assistant_msg("掷出来是6")],
        llm_resp_text="掷出来是6")
    api = webapi_mod.SessionFlowWebApi(st, nm, rec, legacy_mod.LegacyImporter(st, nm))
    return rec, st, nm, api, rid


class TestWebApi(unittest.TestCase):
    def test_handlers_run_off_loop(self):
        # S6：读库不能堵事件循环，handle_xxx 要在线程里跑
        import threading
        _, st, _, api, _ = seed()
        seen = {}
        orig = st.global_stats

        def spy():
            seen["tid"] = threading.get_ident()
            return orig()

        st.global_stats = spy
        run(api.handle_overview({}, {}))
        self.assertIn("tid", seen)
        self.assertNotEqual(seen["tid"], threading.get_ident())
        st.close()
    def test_overview(self):
        _, st, _, api, _ = seed()
        res = run(api.handle_overview({}, {}))
        self.assertEqual(res["status"], "ok")
        for k in ("stats", "platforms", "tool_top", "umos"):
            self.assertIn(k, res["data"])
        self.assertTrue(any(u["umo"] == fakes.UMO_WC for u in res["data"]["umos"]))
        st.close()

    def test_runs(self):
        _, st, _, api, rid = seed()
        bad = run(api.handle_runs({}, {}))
        self.assertEqual(bad["status"], "error")
        res = run(api.handle_runs({"umo": fakes.UMO_WC}, {}))
        self.assertEqual(res["status"], "ok")
        ids = [r["run_id"] for r in res["data"]["runs"]]
        self.assertIn(rid, ids)
        row = next(r for r in res["data"]["runs"] if r["run_id"] == rid)
        self.assertIn("dice", row["tools_used"])
        st.close()

    def test_run_and_sysprompt(self):
        _, st, _, api, rid = seed()
        self.assertEqual(run(api.handle_run({}, {}))["status"], "error")
        self.assertEqual(run(api.handle_run({"id": "nope"}, {}))["status"], "error")
        res = run(api.handle_run({"id": rid}, {}))
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["data"]["events"])
        kinds = {e["kind"] for e in res["data"]["events"]}
        self.assertTrue({"user", "tool_call", "text"} & kinds)
        sp = res["data"]["sysprompt"]
        self.assertIsNotNone(sp)
        sp2 = run(api.handle_sysprompt({"hash": sp["hash"]}, {}))
        self.assertEqual(sp2["status"], "ok")
        self.assertIn("你是助手", sp2["data"]["text"])
        self.assertEqual(run(api.handle_sysprompt({}, {}))["status"], "error")
        st.close()

    def test_search_active_summary(self):
        _, st, _, api, _ = seed()
        self.assertEqual(run(api.handle_search({}, {}))["status"], "error")
        res = run(api.handle_search({"q": "tool:dice"}, {}))
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["data"]["hits"])
        res2 = run(api.handle_active({}, {}))
        self.assertEqual(res2["status"], "ok")
        self.assertIsInstance(res2["data"]["runs"], list)
        self.assertEqual(run(api.handle_umo_summary({}, {}))["status"], "error")
        res3 = run(api.handle_umo_summary({"umo": fakes.UMO_WC}, {}))
        self.assertEqual(res3["status"], "ok")
        self.assertEqual(res3["data"]["runs"], 1)
        st.close()

    def test_legacy_and_admin(self):
        _, st, _, api, _ = seed()
        res = run(api.handle_legacy_status({}, {}))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["data"]["state"], "idle")
        no_api = webapi_mod.SessionFlowWebApi(st, None)
        self.assertEqual(run(no_api.handle_legacy_import({}, {}))["status"], "error")
        self.assertEqual(run(no_api.handle_legacy_status({}, {}))["status"], "error")
        self.assertEqual(run(api.handle_admin_stats({}, {}))["status"], "ok")
        bad = run(api.handle_admin_purge({}, {"days": 0}))
        self.assertEqual(bad["status"], "error")
        okp = run(api.handle_admin_purge({}, {"days": 36500}))
        self.assertEqual(okp["status"], "ok")
        st.close()


if __name__ == "__main__":
    unittest.main()
