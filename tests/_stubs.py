# -*- coding: utf-8 -*-
"""最小 astrbot 桩：让 main.py 能离线 import。只仿形状，不仿行为。"""

from __future__ import annotations

import sys
import types


class _StubLogger:
    """宿主 logger 的桩：只收消息，不输出。"""

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _add(self, level, msg, *a, **k):
        self.records.append((level, str(msg)))

    def debug(self, msg, *a, **k): self._add("debug", msg)
    def info(self, msg, *a, **k): self._add("info", msg)
    def warning(self, msg, *a, **k): self._add("warning", msg)
    def error(self, msg, *a, **k): self._add("error", msg)
    def exception(self, msg, *a, **k): self._add("error", msg)


def _deco(*dargs, **dkw):
    def wrap(fn):
        return fn
    return wrap


def install_astrbot_stub() -> None:
    if "astrbot" in sys.modules and getattr(sys.modules["astrbot"], "_is_stub", False):
        return
    astrbot = types.ModuleType("astrbot")
    astrbot._is_stub = True
    api = types.ModuleType("astrbot.api")
    api.logger = _StubLogger()
    event = types.ModuleType("astrbot.api.event")
    filt = types.ModuleType("astrbot.api.event.filter")
    for name in ("on_waiting_llm_request", "on_llm_request", "on_agent_begin",
                 "on_using_llm_tool", "on_llm_tool_respond", "on_llm_response",
                 "on_agent_done", "on_decorating_result", "on_plugin_error",
                 "llm_tool", "command", "command_group", "permission_type"):
        setattr(filt, name, _deco)
    event.filter = filt
    event.AstrMessageEvent = object
    provider = types.ModuleType("astrbot.api.provider")
    star = types.ModuleType("astrbot.api.star")
    star.Context = object

    class StarStub:
        def __init__(self, context=None):
            self.context = context

    class StarToolsStub:
        data_root = None

        @classmethod
        def get_data_dir(cls, name=None):
            import pathlib
            p = pathlib.Path(cls.data_root) / (name or "plugin")
            p.mkdir(parents=True, exist_ok=True)
            return str(p)

    star.Star = StarStub
    star.StarTools = StarToolsStub
    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    apath = types.ModuleType("astrbot.core.utils.astrbot_path")
    apath.get_astrbot_data_path = lambda: ""
    for name, mod in {
        "astrbot": astrbot, "astrbot.api": api,
        "astrbot.api.event": event, "astrbot.api.event.filter": filt,
        "astrbot.api.provider": provider, "astrbot.api.star": star,
        "astrbot.core": core, "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": apath,
    }.items():
        sys.modules[name] = mod


install = install_astrbot_stub
