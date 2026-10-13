# -*- coding: utf-8 -*-
"""会话流管理页本地预览 / 冒烟用的小服务器（不需要 AstrBot）。

    python tests\\ui_harness.py [端口]

- 在工作区内临时目录建库，用 fakes.drive_run 造 live 回合，
  再从 tests/data/legacy_fixture.db 导一份 legacy 旧档；
- /page/ 下提供 pages/session_flow 的页面，把 AstrBot 的桥接脚本换成 /mock-bridge.js；
- 假桥接把 apiGet / apiPost 转成 fetch("/x/<接口>")，再交给 sf/webapi.py 的处理函数。
只监听 127.0.0.1。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import _stubs  # noqa: E402
import fakes  # noqa: E402
import make_fixture  # noqa: E402

_stubs.install_astrbot_stub()

from sf import legacy as legacy_mod  # noqa: E402
from sf.webapi import SessionFlowWebApi  # noqa: E402

PAGE_DIR = os.path.join(ROOT, "pages", "session_flow")

MOCK_BRIDGE = r"""
window.AstrBotPluginPage = (function () {
  async function call(method, ep, body, params) {
    let url = "/x/" + ep;
    if (params && Object.keys(params).length) url += (url.includes("?") ? "&" : "?") + new URLSearchParams(params);
    const opt = { method };
    if (method === "POST") { opt.body = JSON.stringify(body || {}); opt.headers = { "Content-Type": "application/json" }; }
    const r = await fetch(url, opt);
    const j = await r.json();
    if (j.status === "error") throw new Error(j.message);
    return j.data;
  }
  return {
    ready: () => Promise.resolve({ locale: "zh-CN", isDark: false }),
    apiGet: (ep, params) => call("GET", ep, null, params),
    apiPost: (ep, body) => call("POST", ep, body),
  };
})();
"""

CALL = "call_9"


def build_app():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    run = lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result(120)  # noqa: E731

    rec, st, nm, db_path = fakes.make_recorder()
    # ---- live 数据：工具轮 + 报错轮 + 普通轮 ----
    fakes.drive_run(
        rec, span="h1", prompt="掷个20面骰子",
        tool_uses=[("sf_probe_dice", '{"sides": 20}', "8（20 面）")],
        new_messages=[
            fakes.assistant_msg("", think="调工具算一下",
                                tool_calls=[fakes.tool_call_msg(CALL, "sf_probe_dice", '{"sides": 20}')]),
            fakes.tool_msg("sf_probe_dice 结果：8（20 面）", CALL),
            fakes.assistant_msg("掷出来是 8")],
        llm_resp_text="掷出来是 8")
    fakes.drive_run(
        rec, span="h2", prompt="试试坏工具",
        tool_uses=[("sf_probe_fail", "{}", None)],
        new_messages=[
            fakes.assistant_msg("", tool_calls=[fakes.tool_call_msg(CALL, "sf_probe_fail", "{}")]),
            fakes.tool_msg("error: sf_probe_fail 故意抛错：本地预览", CALL),
            fakes.assistant_msg("工具报错了，先这样")],
        llm_resp_text="工具报错了，先这样")
    fakes.drive_run(
        rec, span="h3", prompt="今天天气怎么样",
        new_messages=[fakes.assistant_msg("今天晴，适合出门")],
        llm_resp_text="今天晴，适合出门")
    # ---- legacy 旧档 ----
    imp = legacy_mod.LegacyImporter(st, nm)
    imp.start(str(make_fixture.build()))

    api = SessionFlowWebApi(st, nm, rec, imp)
    routes = {route: (handler, methods) for route, handler, methods in api.routes()}
    return api, routes, run, db_path


API, ROUTES, RUN, DB_PATH = build_app()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/mock-bridge.js":
            return self._send(200, MOCK_BRIDGE.encode("utf-8"), "text/javascript; charset=utf-8")
        if url.path.startswith("/page/"):
            name = url.path[len("/page/"):] or "index.html"
            path = os.path.normpath(os.path.join(PAGE_DIR, name))
            if not path.startswith(PAGE_DIR) or not os.path.isfile(path):
                return self._send(404, b"not found", "text/plain")
            data = open(path, "rb").read()
            if name == "index.html":
                data = data.replace(b"/api/plugin/page/bridge-sdk.js", b"/mock-bridge.js")
            ctype = {"html": "text/html", "css": "text/css", "js": "text/javascript"}[name.rsplit(".", 1)[-1]]
            return self._send(200, data, ctype + "; charset=utf-8")
        if url.path.startswith("/x/"):
            return self._api("GET", url)
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        return self._api("POST", urlsplit(self.path))

    def _api(self, method, url):
        route = url.path[len("/x/"):]
        if route not in ROUTES or method not in ROUTES[route][1]:
            return self._json({"status": "error", "message": f"未找到该路由 {route}"})
        args = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            body = json.loads(raw or b"{}")
        try:
            self._json(RUN(ROUTES[route][0](args, body)))
        except Exception as exc:
            self._json({"status": "error", "message": str(exc)})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"UI harness: http://127.0.0.1:{port}/page/index.html   数据 {DB_PATH}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
