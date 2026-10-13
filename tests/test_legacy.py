# -*- coding: utf-8 -*-
"""legacy.LegacyImporter 离线用例：用 tests/data 合成库（make_fixture）。"""

from __future__ import annotations

import unittest

import fakes
import make_fixture
from sf import legacy as legacy_mod


def make_importer():
    rec, st, nm, _ = fakes.make_recorder()
    return legacy_mod.LegacyImporter(st, nm), st, str(make_fixture.build())


class TestLegacyImport(unittest.TestCase):
    def test_import_all(self):
        imp, st, fixture = make_importer()
        res = imp.start(fixture)
        self.assertEqual(res["state"], "done")
        # wc 2 轮 + qq 1 轮 + est 1 轮
        self.assertEqual(res["imported_runs"], 4)
        r0 = st.get_run_row("legacy:wc-cid-1:0")
        self.assertEqual(r0["source"], "legacy")
        self.assertEqual(r0["token_in"], 3424)
        self.assertEqual(r0["time_source"], "checkpoint")
        r1 = st.get_run_row("legacy:wc-cid-1:1")
        self.assertEqual(r1["status"], "error")  # stats error
        re_ = st.get_run_row("legacy:est-cid-1:0")
        self.assertEqual(re_["time_source"], "estimated")
        st.close()

    def test_rerun_skips(self):
        imp, st, fixture = make_importer()
        imp.start(fixture)
        res2 = imp.start(fixture)
        self.assertEqual(res2["state"], "done")
        self.assertEqual(res2["imported_runs"], 0)
        self.assertGreater(res2["skipped"], 0)
        st.close()

    def test_force_reimport(self):
        imp, st, fixture = make_importer()
        imp.start(fixture)
        res = imp.start(fixture, force=True)
        self.assertEqual(res["state"], "done")
        self.assertEqual(res["imported_runs"], 4)  # 删了重导，不翻倍
        n = st._read_conn().execute(
            "SELECT COUNT(*) FROM runs WHERE run_id='legacy:est-cid-1:0'").fetchone()[0]
        self.assertEqual(n, 1)
        st.close()

    def test_dedup_against_live(self):
        # 同 cid 已有实时运行且原话/回复前 200 字相同 → 跳过
        rec, st, nm, _ = fakes.make_recorder()
        imp = legacy_mod.LegacyImporter(st, nm)
        fixture = str(make_fixture.build())
        st.touch_umo(make_fixture.EST_UMO, "default", "FriendMessage", "x")
        st.insert_run({"run_id": "live1", "umo": make_fixture.EST_UMO, "cid": "est-cid-1",
                       "source": "live", "status": "done", "sort_at": 1790000000.0, "ord": 0,
                       "user_text": "孤零零一句", "reply_text": "孤零零一回"})
        res = imp.start(fixture)
        got = st.get_run_row("legacy:est-cid-1:0")
        self.assertIsNone(got)
        self.assertGreater(res["skipped"], 0)
        st.close()

    def test_image_stripped_and_stats_time(self):
        imp, st, fixture = make_importer()
        imp.start(fixture)
        r1 = st.get_run_row("legacy:wc-cid-1:1")
        evs = st.events_for("legacy:wc-cid-1:1")
        user_ev = next(e for e in evs if e["kind"] == "user")
        self.assertIn("base64 图片已省略", user_ev["text"])
        self.assertNotIn("iVBOR", user_ev["text"])
        qq = st.get_run_row("legacy:qq-cid-1:0")
        self.assertEqual(qq["time_source"], "provider_stats")
        self.assertEqual(qq["token_in"], 500)
        st.close()

    def test_missing_db(self):
        rec, st, nm, _ = fakes.make_recorder()
        imp = legacy_mod.LegacyImporter(st, nm)
        res = imp.start("Z:\\不存在\\astrbot.db")
        self.assertEqual(res["state"], "error")
        self.assertFalse(imp.is_running())
        st.close()

    def test_checkpoints_filtered_by_user(self):
        # S10：别家会话的同名 checkpoint 行不能污染本轮时间
        import json
        import sqlite3
        path = make_fixture.build(fakes.tmp_db_path("-fx.db"))
        db = sqlite3.connect(str(path))
        db.execute("INSERT INTO platform_message_history(platform_id,user_id,sender_id,sender_name,content,llm_checkpoint_id,created_at)"
                   " VALUES (?,?,?,?,?,?,?)",
                   ("webchat", "webchat:FriendMessage:别人", "u9", "",
                    json.dumps([{"type": "text", "text": "污染"}], ensure_ascii=False),
                    "ck-fixture-1", "2000-01-01 00:00:00.000"))
        db.commit()
        db.close()
        src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        ck = legacy_mod.LegacyImporter._checkpoints(
            src, {"platform_message_history"}, "wc-cid-1", make_fixture.WC_UMO)
        src.close()
        self.assertIn("ck-fixture-1", ck)
        self.assertEqual(ck["ck-fixture-1"]["user_at"], "2026-10-12 23:33:29.609")


if __name__ == "__main__":
    unittest.main()
