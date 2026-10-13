# -*- coding: utf-8 -*-
"""诊断日志与导出：日志落盘 / 滚动 / 钩子统计 / 打码 / 诊断包不含聊天正文。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import fakes
from sf import diag as diag_mod
from sf.webapi import SessionFlowWebApi


class FakeHost:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def info(self, m): self.calls.append(("info", m))
    def warning(self, m): self.calls.append(("warning", m))
    def error(self, m): self.calls.append(("error", m))
    def debug(self, m): self.calls.append(("debug", m))


def new_diag(**kw):
    d = Path(tempfile.mkdtemp(prefix="sfdiag-"))
    host = kw.pop("host", FakeHost())
    return diag_mod.DiagLog(host, file_path=d / "logs" / "session_flow.log", **kw), host, d


def run(coro):
    return asyncio.run(coro)


class TestMask(unittest.TestCase):
    def test_mask_hides_id_keeps_shape(self):
        m = diag_mod.mask_umo("default:GroupMessage:100000999")
        self.assertTrue(m.startswith("default:GroupMessage:#"))
        self.assertNotIn("100000999", m)
        self.assertEqual(m, diag_mod.mask_umo("default:GroupMessage:100000999"))  # 稳定
        self.assertNotEqual(m, diag_mod.mask_umo("default:GroupMessage:100000998"))
        self.assertEqual(diag_mod.mask_umo(""), "")


class TestDiagLog(unittest.TestCase):
    def test_file_ring_and_host(self):
        d, host, tmp = new_diag()
        d.info("[session_flow] 你好")           # 带前缀的不重复加前缀
        d.warning("注意")
        d.close()
        text = (tmp / "logs" / "session_flow.log").read_text(encoding="utf-8")
        self.assertIn("INFO", text)
        self.assertIn("你好", text)
        self.assertNotIn("[session_flow]", text)
        self.assertEqual(host.calls[0], ("info", "[session_flow] 你好"))
        self.assertEqual(host.calls[1], ("warning", "[session_flow] 注意"))
        self.assertEqual(len(d.recent(10)), 2)

    def test_exception_keeps_traceback_and_counts(self):
        d, host, _ = new_diag()
        try:
            raise ValueError("炸了")
        except ValueError:
            d.exception("某处出错")
        self.assertEqual(d.error_count, 1)
        self.assertIn("ValueError: 炸了", d.errors[-1]["traceback"])
        self.assertTrue(any(k == "error" and "Traceback" in m for k, m in host.calls))
        d.close()

    def test_run_note_not_to_host_and_switch(self):
        d, host, _ = new_diag()
        d.run_note("运行开始 x")
        self.assertEqual(host.calls, [])
        self.assertEqual(len(d.recent(5)), 1)
        d.close()
        d2, _, _ = new_diag(log_runs=False)
        d2.run_note("运行开始 y")
        self.assertEqual(d2.recent(5), [])
        d2.close()

    def test_disabled_writes_nothing_but_still_forwards(self):
        d, host, tmp = new_diag(enabled=False)
        d.info("x")
        self.assertEqual(d.recent(5), [])
        self.assertFalse((tmp / "logs" / "session_flow.log").exists())
        self.assertEqual(len(host.calls), 1)

    def test_rotation_and_tail_across_files(self):
        d, _, tmp = new_diag(max_bytes=600, backups=3)
        for i in range(60):
            d.info(f"第 {i:03d} 行 " + "x" * 30)
        d.close()
        files = sorted(p.name for p in (tmp / "logs").iterdir())
        self.assertGreater(len(files), 1, files)  # 滚动出了旧文件
        d2 = diag_mod.DiagLog(None, file_path=tmp / "logs" / "session_flow.log")
        tail = d2.tail_file(1000)
        self.assertIn("第 059 行", tail[-1])
        self.assertLess(tail.index(next(x for x in tail if "第 055 行" in x)), len(tail) - 1)  # 旧的在前
        d2.close()

    def test_close_releases_file(self):
        d, _, tmp = new_diag()
        d.info("x")
        d.close()
        (tmp / "logs" / "session_flow.log").unlink()  # Windows 下句柄没放会抛

    def test_write_after_close_is_dropped_quietly(self):
        d, host, tmp = new_diag()
        d.info("关之前")
        d.close()
        d.info("关之后")                                   # 不抛，也不会把文件又打开
        text = (tmp / "logs" / "session_flow.log").read_text(encoding="utf-8")
        self.assertIn("关之前", text)
        self.assertNotIn("关之后", text)
        (tmp / "logs" / "session_flow.log").unlink()

    def test_file_lines_are_on_disk_before_close(self):
        d, _, tmp = new_diag()
        d.info("崩溃前最后一行")
        text = (tmp / "logs" / "session_flow.log").read_text(encoding="utf-8")  # 没 close 也读得到
        self.assertIn("崩溃前最后一行", text)
        d.close()


class TestRotatingFile(unittest.TestCase):
    """手写的滚动文件（不用 logging）：留几份、最老的丢、改名失败时的兜底。"""

    def _rf(self, **kw):
        tmp = Path(tempfile.mkdtemp(prefix="sfrot-"))
        return diag_mod._RotatingFile(str(tmp / "logs" / "a.log"), **kw), tmp / "logs"

    def test_keeps_at_most_backups_and_drops_oldest(self):
        rf, logs = self._rf(max_bytes=100, backups=2)
        for i in range(40):
            rf.write_line(f"行{i:03d}" + "y" * 20)
        rf.close()
        names = sorted(p.name for p in logs.iterdir())
        self.assertEqual(names, ["a.log", "a.log.1", "a.log.2"])       # 没有 .3
        newest = (logs / "a.log").read_text(encoding="utf-8")
        self.assertIn("行039", newest)
        old = (logs / "a.log.2").read_text(encoding="utf-8")
        self.assertNotIn("行000", old)                                   # 最老的那份被丢了
        for p in logs.iterdir():
            self.assertLessEqual(p.stat().st_size, 100 + 40)

    def test_no_backups_just_truncates(self):
        rf, logs = self._rf(max_bytes=100, backups=0)
        for i in range(30):
            rf.write_line(f"行{i:03d}" + "y" * 20)
        rf.close()
        self.assertEqual([p.name for p in logs.iterdir()], ["a.log"])
        self.assertIn("行029", (logs / "a.log").read_text(encoding="utf-8"))

    def test_rename_failure_keeps_appending_and_reports(self):
        import os
        from unittest import mock
        rf, logs = self._rf(max_bytes=100, backups=2)
        with mock.patch.object(os, "replace", side_effect=PermissionError("被占用")):
            for i in range(8):                       # 8 行 ≈ 200 字节：超过 100，没到 400
                rf.write_line(f"行{i:03d}" + "y" * 20)
            self.assertIn("PermissionError", rf.rotate_error or "")
            text = (logs / "a.log").read_text(encoding="utf-8")
            self.assertIn("行000", text)             # 滚不动，没丢数据
            self.assertIn("行007", text)
            for i in range(8, 40):                   # 一直滚不动，到 4 倍上限就清空重写
                rf.write_line(f"行{i:03d}" + "y" * 20)
            self.assertLessEqual((logs / "a.log").stat().st_size, 100 * rf.HARD_FACTOR + 40)
            self.assertIn("行039", (logs / "a.log").read_text(encoding="utf-8"))
        rf.write_line("改名恢复了")                    # 之后能改名了，滚动恢复，错误清掉
        rf.write_line("z" * 120)
        self.assertIsNone(rf.rotate_error)
        rf.close()

    def test_write_failure_is_recorded_not_raised(self):
        d, host, tmp = new_diag()
        d.info("先写一行")
        d._file.close()
        logs = tmp / "logs"
        (logs / "session_flow.log").unlink()
        (logs / "session_flow.log").mkdir()            # 把日志文件的位置换成一个目录，下次打开必然失败
        d.info("写不进去")                            # 不能抛
        self.assertIsNotNone(d.file_error)
        self.assertEqual(len(d.recent(10)), 2)         # 内存环不受影响
        self.assertTrue(any("写不进去" in m for _, m in host.calls))   # 宿主那份也照给
        d.close()

    def test_unwritable_dir_reports_file_error_at_start(self):
        tmp = Path(tempfile.mkdtemp(prefix="sfdiag-"))
        (tmp / "logs").write_text("我是个文件，不是目录", encoding="utf-8")
        d = diag_mod.DiagLog(FakeHost(), file_path=tmp / "logs" / "session_flow.log")
        self.assertIsNotNone(d.file_error)
        d.info("没有文件也能记内存环")
        self.assertEqual(len(d.recent(5)), 1)
        d.close()


class TestGuard(unittest.TestCase):
    def test_counts_and_trace_masked(self):
        d, _, _ = new_diag()
        with d.guard("agent_done", "default:GroupMessage:100000999"):
            pass
        with d.guard("agent_done", "default:GroupMessage:100000999"):
            pass
        st = d.hook_stats()["agent_done"]
        self.assertEqual((st["count"], st["errors"]), (2, 0))
        tr = d.trace_list()
        self.assertEqual(len(tr), 2)
        self.assertNotIn("100000999", json.dumps(tr))
        d.close()

    def test_exception_swallowed_and_recorded(self):
        d, host, _ = new_diag()
        with d.guard("decorating", "u"):
            raise RuntimeError("钩子里炸了")
        st = d.hook_stats()["decorating"]
        self.assertEqual(st["errors"], 1)
        self.assertIn("RuntimeError", st["last_error"])
        self.assertEqual(d.error_count, 1)
        self.assertTrue(any("decorating 钩子出错" in m for _, m in host.calls))
        d.close()

    def test_cancelled_error_not_swallowed(self):
        d, _, _ = new_diag()
        with self.assertRaises(asyncio.CancelledError):
            with d.guard("x"):
                raise asyncio.CancelledError()
        d.close()


class TestRecorderRunNotes(unittest.TestCase):
    def test_open_close_lines_masked(self):
        d, _, _ = new_diag()
        rec, st, nm, _ = fakes.make_recorder(log=d)
        fakes.drive_run(rec, umo=fakes.UMO_QQ, span="sp-a", prompt="掷个骰子",
                        new_messages=[fakes.assistant_msg("好")])
        lines = d.recent(20)
        self.assertTrue(any("运行开始 sp-a#1" in x for x in lines), lines)
        end = [x for x in lines if "运行结束 sp-a#1" in x]
        self.assertTrue(end and "状态 done" in end[0], lines)
        self.assertNotIn("100000999", "\n".join(lines))   # 会话 ID 不进日志
        st.close()
        d.close()

    def test_watchdog_interrupt_logged(self):
        d, _, _ = new_diag()
        import time
        clock = {"t": time.time()}
        rec, st, nm, _ = fakes.make_recorder(log=d)
        rec.clock = lambda: clock["t"]
        ev = fakes.FakeEvent(umo=fakes.UMO_WC, span_id="sp-w")
        rec.on_waiting(ev)
        rec.on_request_last(ev, fakes.FakeReq(prompt="x", system_prompt="s", cid="c"))
        clock["t"] += 3600
        ids = rec.watchdog_interrupt_stale()
        self.assertEqual(len(ids), 1)
        self.assertTrue(any("运行结束" in x and "状态 interrupted" in x for x in d.recent(20)))
        st.close()
        d.close()


class TestBundle(unittest.TestCase):
    SECRET_TEXT = "机密正文XYZ不该出现在诊断包里"

    def _setup(self):
        d, _, tmp = new_diag()
        rec, st, nm, _ = fakes.make_recorder(log=d)
        fakes.drive_run(rec, umo=fakes.UMO_QQ, span="sp-1", prompt=self.SECRET_TEXT,
                        new_messages=[fakes.assistant_msg(self.SECRET_TEXT + "的回复")])
        api = SessionFlowWebApi(st, nm, recorder=rec)
        api.diag = d
        api.config_probe = lambda: {"enable_record": True, "api_key": "sk-abc123", "exclude_umos": [fakes.UMO_WC]}
        api.runtime_probe = lambda: {"write_queue": 0, "tasks": [{"name": "sf-writer", "done": False}]}
        return api, st, d

    def test_bundle_shape_and_no_chat_text(self):
        api, st, d = self._setup()
        res = run(api.handle_diag({}, {}))
        self.assertEqual(res["status"], "ok")
        b = res["data"]
        for k in ("format", "generated_at", "env", "config", "db", "recent_runs", "runtime",
                  "hooks", "hook_trace", "recent_errors", "log_tail", "privacy"):
            self.assertIn(k, b)
        text = json.dumps(b, ensure_ascii=False)
        self.assertNotIn("机密正文XYZ", text)            # 聊天正文不进诊断包
        self.assertNotIn("100000999", text)              # 会话 ID 默认打码
        self.assertNotIn("test-uuid-0001", text)
        self.assertEqual(b["config"]["api_key"], "<已隐藏>")
        self.assertNotIn("umo_map", b)
        self.assertEqual(b["db"]["runs"], 1)
        self.assertEqual(b["recent_runs"][0]["status"], "done")
        self.assertIn("tokens_cached", b["recent_runs"][0])       # tokens_in 只是没命中缓存的那部分，要和它并排看
        self.assertTrue(any("运行结束" in x for x in b["log_tail"]))
        st.close()
        d.close()

    def test_raw_adds_umo_map(self):
        api, st, d = self._setup()
        b = run(api.handle_diag({"raw": "1"}, {}))["data"]
        self.assertIn("umo_map", b)
        self.assertIn(fakes.UMO_QQ, b["umo_map"].values())
        self.assertNotIn("机密正文XYZ", json.dumps(b, ensure_ascii=False))  # raw 也不带正文
        st.close()
        d.close()

    def test_no_diag_returns_error(self):
        rec, st, nm, _ = fakes.make_recorder()
        res = run(SessionFlowWebApi(st, nm, recorder=rec).handle_diag({}, {}))
        self.assertEqual(res["status"], "error")
        st.close()

    def test_api_errors_land_in_diag_log_and_host(self):
        """接口里的报错走诊断日志：进内存环 / 文件 / “最近报错”，也转给宿主 logger。"""
        from unittest import mock
        from sf import search as search_mod
        api, st, d = self._setup()
        host = d.host
        with mock.patch.object(search_mod, "search", side_effect=RuntimeError("检索炸了")):
            res = run(api.handle_search({"q": "随便"}, {}))
        self.assertEqual(res["status"], "error")
        self.assertEqual(d.error_count, 1)
        self.assertIn("RuntimeError: 检索炸了", d.errors[-1]["traceback"])
        self.assertTrue(any("检索出错" in x for x in d.recent(10)))
        self.assertTrue(any(k == "error" and "检索出错" in m for k, m in host.calls))
        b = run(api.handle_diag({}, {}))["data"]
        self.assertTrue(any("检索出错" in e["message"] for e in b["recent_errors"]))
        self.assertIn("rotate_error", b["log_file"])
        st.close()
        d.close()

    def test_api_errors_without_diag_do_not_raise(self):
        from unittest import mock
        from sf import search as search_mod
        rec, st, nm, _ = fakes.make_recorder()
        api = SessionFlowWebApi(st, nm, recorder=rec)     # 没接诊断日志（离线）
        with mock.patch.object(search_mod, "search", side_effect=RuntimeError("x")):
            res = run(api.handle_search({"q": "随便"}, {}))
        self.assertEqual(res["status"], "error")
        st.close()


if __name__ == "__main__":
    unittest.main()
