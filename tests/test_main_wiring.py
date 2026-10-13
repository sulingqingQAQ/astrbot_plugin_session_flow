# -*- coding: utf-8 -*-
"""main.py 接线（用 astrbot 桩离线跑）：日志落盘、钩子统计、诊断包、关闭释放文件。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import fakes
import _stubs

ROOT = Path(__file__).resolve().parent.parent


def load_main():
    _stubs.install_astrbot_stub()
    name = "sf_plugin_under_test"
    for k in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[k]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[name] = pkg
    spec.loader.exec_module(pkg)
    return importlib.import_module(name + ".main")


class FakeContext:
    def get_db(self):
        raise RuntimeError("离线没有宿主库")


class TestMainWiring(unittest.TestCase):
    def test_end_to_end(self):
        main = load_main()
        data_root = Path(tempfile.mkdtemp(prefix="sfmain-"))
        sys.modules["astrbot.api.star"].StarTools.data_root = str(data_root)
        plugin = main.SessionFlowPlugin(FakeContext(), {"enable_record": True, "exclude_umos": [fakes.UMO_QQ]})

        async def go():
            await plugin.initialize()
            ev = fakes.FakeEvent(umo=fakes.UMO_WC, span_id="sp-main")
            await plugin._sf_waiting(ev)
            req = fakes.FakeReq(prompt="机密正文XYZ", system_prompt="你是助手", cid="c1")
            await plugin._sf_req_first(ev, req)
            await plugin._sf_req_last(ev, req)
            msgs = [{"role": "system", "content": "你是助手"}, fakes.user_msg("机密正文XYZ")]
            ctx = fakes.FakeCtx(msgs)
            await plugin._sf_begin(ev, ctx)
            await plugin._sf_llm_resp(ev, fakes.FakeResp("好"))
            msgs.append(fakes.assistant_msg("好"))
            await plugin._sf_done(ev, ctx, fakes.FakeResp("好"))
            await asyncio.sleep(0.3)  # 让写库协程消化队列
            res = await plugin.webapi.handle_diag({}, {})
            await plugin.terminate()
            return res

        res = asyncio.run(go())
        self.assertEqual(res["status"], "ok", res)
        b = res["data"]
        # 日志文件落在插件数据目录的 logs 下
        log_file = Path(plugin.data_dir) / "logs" / "session_flow.log"
        self.assertTrue(log_file.exists())
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("启动：插件", text)
        self.assertIn("运行开始 sp-main#1", text)
        self.assertIn("运行结束 sp-main#1", text)
        self.assertIn("已停止", text)
        self.assertNotIn("机密正文XYZ", text)
        self.assertNotIn("test-uuid-0001", text)       # 会话 ID 不进日志
        self.assertIn("1 条", text)                      # exclude_umos 只记条数
        # 钩子统计：每个钩子至少被记过一次，没有报错
        for hook in ("waiting", "llm_request(高)", "llm_request(低)", "agent_begin", "llm_response", "agent_done"):
            self.assertEqual(b["hooks"][hook]["count"], 1, hook)
            self.assertEqual(b["hooks"][hook]["errors"], 0, hook)
        # 环境 + 后台任务
        meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        expected = re.search(r'^version:\s*"?([^"\r\n]+)', meta, re.M).group(1)
        self.assertEqual(b["env"]["plugin_version"], expected)
        self.assertEqual([t["name"] for t in b["runtime"]["tasks"]],
                         ["sf-writer", "sf-watchdog", "sf-align", "sf-retention"])
        self.assertTrue(all(not t["done"] for t in b["runtime"]["tasks"]))
        self.assertEqual(b["db"]["runs"], 1)
        self.assertNotIn("机密正文XYZ", json.dumps(b, ensure_ascii=False))
        # 关闭后文件句柄已释放（Windows 下没放会删不掉）
        log_file.unlink()

    def test_decorating_hook_records_actual_sent_when_it_differs(self):
        # v0.1.5：发送前最后一个钩子里，实际发出的内容和模型原话不一样才补一条“实际发出”
        main = load_main()
        data_root = Path(tempfile.mkdtemp(prefix="sfmain-"))
        sys.modules["astrbot.api.star"].StarTools.data_root = str(data_root)
        plugin = main.SessionFlowPlugin(FakeContext(), {"enable_record": True})

        async def go(sent_text):
            await plugin.initialize()
            ev = fakes.FakeEvent(umo=fakes.UMO_WC, span_id="sp-" + sent_text)
            await plugin._sf_waiting(ev)
            req = fakes.FakeReq(prompt="问", system_prompt="你是助手", cid="c1")
            await plugin._sf_req_first(ev, req)
            await plugin._sf_req_last(ev, req)
            msgs = [{"role": "system", "content": "你是助手"}, fakes.user_msg("问")]
            ctx = fakes.FakeCtx(msgs)
            await plugin._sf_begin(ev, ctx)
            msgs.append(fakes.assistant_msg("原话"))
            await plugin._sf_llm_resp(ev, fakes.FakeResp("原话"))
            await plugin._sf_done(ev, ctx, fakes.FakeResp("原话"))
            ev.set_result(fakes.FakeResult(sent_text, ctype="LLM_RESULT", is_llm=True))
            await plugin._sf_decorating(ev)
            await asyncio.sleep(0.3)

        async def both():
            await go("原话")          # 一样：不补
            await go("改过的话")      # 不一样：补一条
            kinds = {rid: [e["kind"] for e in plugin.store.events_for(rid)] for rid in ("sp-原话#1", "sp-改过的话#1")}
            hooks = plugin.diag.hook_stats()
            await plugin.terminate()
            return kinds, hooks

        kinds, hooks = asyncio.run(both())
        self.assertNotIn("sent", kinds["sp-原话#1"])
        self.assertIn("sent", kinds["sp-改过的话#1"])
        self.assertEqual((hooks["decorating"]["count"], hooks["decorating"]["errors"]), (2, 0))

    def test_hook_error_is_swallowed_and_counted(self):
        main = load_main()
        data_root = Path(tempfile.mkdtemp(prefix="sfmain-"))
        sys.modules["astrbot.api.star"].StarTools.data_root = str(data_root)
        plugin = main.SessionFlowPlugin(FakeContext(), {})

        def boom(*a, **k):
            raise RuntimeError("recorder 炸了")
        plugin.recorder.on_waiting = boom

        async def go():
            await plugin._sf_waiting(fakes.FakeEvent())  # 不能抛出来
            return plugin.diag.hook_stats()["waiting"]
        st = asyncio.run(go())
        self.assertEqual((st["count"], st["errors"]), (1, 1))
        self.assertIn("RuntimeError", st["last_error"])
        plugin.diag.close()


if __name__ == "__main__":
    unittest.main()
