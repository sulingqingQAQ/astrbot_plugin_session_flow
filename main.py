# -*- coding: utf-8 -*-
"""会话流插件入口：钩子 → recorder，接口注册，后台任务启停。"""

from __future__ import annotations

import asyncio
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .sf import attrib as attrib_mod
from .sf import diag as diag_mod
from .sf import health as health_mod
from .sf import legacy as legacy_mod
from .sf import names as names_mod
from .sf import recorder as recorder_mod
from .sf import store as store_mod
from .sf import webapi as webapi_mod

PLUGIN_NAME = "astrbot_plugin_session_flow"


def _cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    try:
        if isinstance(cfg, dict):
            return cfg.get(key, default)
        return getattr(cfg, key, default)
    except Exception:
        return default


def _astrbot_db_path(context: Context) -> str | None:
    try:
        db = context.get_db()
        p = getattr(db, "db_path", None)
        if p and Path(str(p)).exists():
            return str(p)
    except Exception:
        pass
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        p = Path(get_astrbot_data_path()) / "data_v4.db"
        if p.exists():
            return str(p)
    except Exception:
        pass
    return None


def _umo_of(event: Any) -> str:
    try:
        return str(getattr(event, "unified_msg_origin", "") or "")
    except Exception:
        return ""


def _plugin_version() -> str:
    try:
        text = (Path(__file__).resolve().parent / "metadata.yaml").read_text(encoding="utf-8")
        m = re.search(r'^version:\s*"?([^"\r\n]+)', text, re.M)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def _astrbot_version() -> str:
    try:
        from astrbot.core.config.default import VERSION
        return str(VERSION)
    except Exception:
        return ""


class SessionFlowPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.diag = diag_mod.DiagLog(
            logger, file_path=self.data_dir / "logs" / "session_flow.log",
            enabled=bool(_cfg_get(self.config, "diag_log_enabled", True)),
            log_runs=bool(_cfg_get(self.config, "diag_log_runs", True)))
        self.store = store_mod.Store(self.data_dir / "session_flow.db")
        self.astrbot_db = _astrbot_db_path(context)
        self.names = names_mod.NameResolver(self.astrbot_db)
        self.cfg = recorder_mod.RecorderConfig.from_config(
            self.config, lambda c, k, d=None: _cfg_get(c, k, d))
        self.recorder = recorder_mod.Recorder(self.store, self.names, self.cfg, log=self.diag)
        self.recorder.astrbot_db = self.astrbot_db
        # 注入归属：包装别的插件的 on_llm_request 处理函数（精确）+ 源码字符串匹配（疑似）
        self.attributor = attrib_mod.Attributor(
            self.recorder, own_package=__name__.rsplit(".", 1)[0], log=self.diag)
        self.guesser = attrib_mod.Guesser(attrib_mod.Guesser.default_roots(Path(__file__).resolve().parent))
        self.importer = legacy_mod.LegacyImporter(self.store, self.names, dict(self.config)
                                                  if isinstance(self.config, dict) else {}, log=self.diag)
        self.webapi = webapi_mod.SessionFlowWebApi(
            self.store, self.names, recorder=self.recorder, importer=self.importer)
        # 做 webapi 里 legacy import 用的兜底
        self.webapi.astrbot_db = self.astrbot_db
        # 诊断导出要的东西
        self.webapi.diag = self.diag
        self.webapi.guesser = self.guesser
        self.webapi.config_probe = lambda: dict(self.config) if hasattr(self.config, "items") else {}
        self.webapi.runtime_probe = self._runtime_probe
        self.webapi.plugin_version = _plugin_version()
        self.webapi.health_probe = self._health
        # 写库任务的运行情况（顶部横幅用：卡住 / 失败）
        self._write_stats: dict[str, Any] = {"done": 0, "failed": 0, "busy_since": None, "last_ok_at": None,
                                             "last_fail_at": None, "last_fail": None}
        self._write_q: asyncio.Queue | None = None
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ 生命周期
    async def initialize(self):
        loop = asyncio.get_running_loop()
        self._write_q = asyncio.Queue()
        self.recorder.set_submit(lambda job: self._write_q.put_nowait(job))
        try:
            n = await asyncio.to_thread(self.recorder.recover_interrupted)
            if n:
                self.diag.info(f"启动恢复：{n} 个未关闭运行标成 interrupted")
        except Exception:
            self.diag.exception("启动恢复失败")
        try:
            n = self.webapi.register(self.context, PLUGIN_NAME)
            self.diag.info(f"注册接口 {n} 个")
        except Exception:
            self.diag.exception("注册接口失败")
        self._snapshot_env()
        self._tasks = [
            loop.create_task(self._writer_loop(), name="sf-writer"),
            loop.create_task(self._watchdog_loop(), name="sf-watchdog"),
            loop.create_task(self._align_loop(), name="sf-align"),
            loop.create_task(self._retention_loop(), name="sf-retention"),
        ]
        self.diag.info("就绪")

    def _snapshot_env(self) -> None:
        env = {
            "plugin_version": _plugin_version(), "astrbot_version": _astrbot_version(),
            "python": sys.version.split()[0], "platform": platform.platform(),
            "astrbot_db_found": bool(self.astrbot_db),
            "data_dir": str(self.data_dir),
        }
        self.diag.note("env", env)
        cfg = {k: v for k, v in (dict(self.config).items() if hasattr(self.config, "items") else [])}
        if isinstance(cfg.get("exclude_umos"), (list, tuple)):
            cfg["exclude_umos"] = f"{len(cfg['exclude_umos'])} 条"  # 会话 ID 不进日志
        self.diag.info(
            f"启动：插件 {env['plugin_version']} · AstrBot {env['astrbot_version'] or '未知'} · "
            f"Python {env['python']} · 对齐用的 AstrBot 库{'已找到' if self.astrbot_db else '没找到'} · 配置 {cfg}")

    def _runtime_probe(self) -> dict:
        tasks = []
        for t in self._tasks:
            info = {"name": t.get_name(), "done": t.done(), "cancelled": t.cancelled(), "error": None}
            try:
                if t.done() and not t.cancelled() and t.exception() is not None:
                    info["error"] = repr(t.exception())[:300]
            except Exception:
                pass
            tasks.append(info)
        return {
            "write_queue": self._write_q.qsize() if self._write_q is not None else None,
            "write": dict(self._write_stats),
            "tasks": tasks,
            "attribution": self.attributor.stats(),
        }

    def _health(self) -> list[dict]:
        """页面顶部横幅：自检结果，空列表 = 一切正常。"""
        return health_mod.evaluate(self._runtime_probe(), self.diag.hook_stats(), time.time())

    async def terminate(self):
        try:
            n = self.attributor.restore()
            if n:
                self.diag.info(f"已还原 {n} 个被包装的钩子处理函数")
        except Exception:
            self.diag.exception("还原处理函数包装失败")
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        # 把队列里剩下的写完
        try:
            while self._write_q is not None and not self._write_q.empty():
                job = self._write_q.get_nowait()
                try:
                    await asyncio.to_thread(job, self.store)
                except Exception:
                    self.diag.exception("退出时补写失败")
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass
        try:
            self.names.close()
        except Exception:
            pass
        self.diag.info("已停止")
        self.diag.close()

    # ------------------------------------------------------------ 后台任务
    async def _writer_loop(self):
        assert self._write_q is not None
        ws = self._write_stats
        while True:
            job = await self._write_q.get()
            ws["busy_since"] = time.time()
            try:
                await asyncio.to_thread(job, self.store)
                ws["done"] += 1
                ws["last_ok_at"] = time.time()
            except Exception as exc:
                ws["failed"] += 1
                ws["last_fail_at"] = time.time()
                ws["last_fail"] = f"{type(exc).__name__}: {exc}"[:300]
                self.diag.exception("写库失败")
            finally:
                ws["busy_since"] = None

    async def _watchdog_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                ids = await asyncio.to_thread(self.recorder.watchdog_interrupt_stale)
                self.diag.note("last_watchdog", {"at": diag_mod._iso(self.diag.clock()), "closed": len(ids)})
                if ids:
                    self.diag.info(f"看门狗关闭 {len(ids)} 个过期运行：{', '.join(ids[:5])}")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.diag.exception("看门狗出错")

    async def _align_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                if _cfg_get(self.config, "link_provider_stats", True):
                    res = await asyncio.to_thread(
                        self.recorder.align_provider_stats, self.astrbot_db)
                    self.diag.note("last_align", {"at": diag_mod._iso(self.diag.clock()), **res})
                    if res.get("linked") or res.get("errors"):
                        self.diag.info(f"provider_stats 对齐：检查 {res.get('checked')} 行，对上 {res.get('linked')} 行，补标失败 {res.get('errors')} 行")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.diag.exception("provider_stats 对齐出错")

    async def _retention_loop(self):
        while True:
            await asyncio.sleep(86400)
            try:
                days = int(_cfg_get(self.config, "retention_days", 90) or 0)
                if days > 0:
                    n = await asyncio.to_thread(self.recorder.cleanup_retention, days)
                    self.diag.note("last_retention", {"at": diag_mod._iso(self.diag.clock()), "days": days, "deleted": n})
                    if n:
                        self.diag.info(f"保留期清理 {n} 个运行")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.diag.exception("保留期清理出错")

    # ------------------------------------------------------------ 钩子（只转发，异常绝不外抛）
    @filter.on_waiting_llm_request()
    async def _sf_waiting(self, event: AstrMessageEvent):
        with self.diag.guard("waiting", _umo_of(event)):
            self.recorder.on_waiting(event)

    @filter.on_llm_request(priority=999999)
    async def _sf_req_first(self, event: AstrMessageEvent, req):
        with self.diag.guard("llm_request(高)", _umo_of(event)):
            self.recorder.on_request_first(event, req)
            self.attributor.ensure_wrapped()   # 在比我们优先级低的处理函数跑之前包好

    @filter.on_llm_request(priority=-999999)
    async def _sf_req_last(self, event: AstrMessageEvent, req):
        with self.diag.guard("llm_request(低)", _umo_of(event)):
            self.recorder.on_request_last(event, req)

    @filter.on_agent_begin()
    async def _sf_begin(self, event: AstrMessageEvent, run_context):
        with self.diag.guard("agent_begin", _umo_of(event)):
            self.recorder.on_agent_begin(event, run_context)

    @filter.on_using_llm_tool()
    async def _sf_tool_start(self, event: AstrMessageEvent, tool, tool_args):
        with self.diag.guard("tool_start", _umo_of(event)):
            self.recorder.on_tool_start(event, tool, tool_args)

    @filter.on_llm_tool_respond()
    async def _sf_tool_end(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        with self.diag.guard("tool_respond", _umo_of(event)):
            self.recorder.on_tool_end(event, tool, tool_args, tool_result)

    @filter.on_llm_response()
    async def _sf_llm_resp(self, event: AstrMessageEvent, resp):
        with self.diag.guard("llm_response", _umo_of(event)):
            self.recorder.on_llm_response(event, resp)

    @filter.on_agent_done()
    async def _sf_done(self, event: AstrMessageEvent, run_context, response):
        with self.diag.guard("agent_done", _umo_of(event)):
            self.recorder.on_agent_done(event, run_context, response)

    @filter.on_decorating_result(priority=-999999)
    async def _sf_decorating(self, event: AstrMessageEvent):
        with self.diag.guard("decorating", _umo_of(event)):
            self.recorder.on_decorating(event)
            self.recorder.on_sent(event)   # 发出的内容和模型原话不一样才补一条“实际发出”

    @filter.on_plugin_error()
    async def _sf_plugin_error(self, event, plugin_name: str, handler_name: str,
                               error: Exception, traceback_text: str):
        with self.diag.guard("plugin_error", _umo_of(event)):
            self.recorder.on_plugin_error(event, plugin_name, handler_name, error, traceback_text)
