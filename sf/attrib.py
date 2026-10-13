# -*- coding: utf-8 -*-
"""注入归属：这条注入是被哪个插件的哪个函数塞进来的。

三层，准确度从高到低：
1. 精确（Attributor）：宿主按优先级逐个调用 on_llm_request 的处理函数。会话流在最高优先级的钩子里把
   其他插件登记的处理函数各包一层，包装在处理函数前后各拍一次 system_prompt / prompt / 额外文本块，
   变了就记在这个插件这个函数头上。
2. 更早就在：最高优先级钩子拍第一张快照时已经存在的内容 → 不是任何被包装的处理函数加的，
   只能是 AstrBot 内核，或优先级比会话流还高的插件。
3. 疑似（Guesser）：到已装插件 / 内核源码里找这个标签名的字面量，只给候选，不作定论。

v0.1.5 起不止 on_llm_request：同一套包装还覆盖 on_llm_response（谁改写了模型回复）、on_decorating_result
（谁改了发出前的消息链），请求里除提示词外的历史 / 工具集 / 图片 / 模型被谁改了也记下来；
其余钩子（等待 / agent 开始结束 / 工具前后 / 发送后）只统计次数和耗时。

要伸手进宿主内部：登记表 star_handlers_registry、EventType、star_map（都不是公开 API）。
宿主改了这些，Attributor 会把 status 记成不可用并安静退出，只剩第 2、3 层，不影响记录。

和宿主插件管理器的配合（重要）：宿主（重）载插件时做
    raw = handler.handler.func if isinstance(handler.handler, functools.partial) else handler.handler
    handler.handler = functools.partial(raw, 插件实例)
所以包装必须保持「functools.partial(被包装的原函数, 实例)」的形状：partial.func 是我们的包装函数，
它里面再调原函数，这样宿主重载时重新绑定新实例，不会调到旧实例，也不会叠包装。
"""

from __future__ import annotations

import functools
import inspect
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import extract as ex

_TAG_LITERAL = re.compile(r"<([A-Za-z][\w-]*)>")
_SKIP_DIRS = {"tests", "test", "__pycache__", ".git", "node_modules", "venv", ".venv", "site-packages"}
_NO_GUESS_PREFIX = ("系统提示词", "提问")


def _part_text(part: Any) -> str:
    try:
        return ex.part_text(part) or ""
    except Exception:
        return str(getattr(part, "text", "") or "")


# =================================================================== 精确归属

# 钩子表：(EventType 属性名, 短名, 模式)。模式决定拍什么快照、比出什么改动：
#   request  请求：system_prompt / prompt / 额外文本块（注入）+ 历史 / 工具集 / 图片 / 模型（改动）
#   response 模型回复：completion_text（被谁改写）
#   result   发出前的消息链（被谁改写）
#   None     只记次数和耗时
HOOKS = (
    ("OnLLMRequestEvent", "on_llm_request", "request"),
    ("OnLLMResponseEvent", "on_llm_response", "response"),
    ("OnDecoratingResultEvent", "on_decorating_result", "result"),
    ("OnWaitingLLMRequestEvent", "on_waiting_llm_request", None),
    ("OnAgentBeginEvent", "on_agent_begin", None),
    ("OnAgentDoneEvent", "on_agent_done", None),
    ("OnUsingLLMToolEvent", "on_using_llm_tool", None),
    ("OnLLMToolRespondEvent", "on_llm_tool_respond", None),
    ("OnAfterMessageSentEvent", "on_after_message_sent", None),
)
_MODE = {short: mode for _, short, mode in HOOKS}


class Attributor:
    def __init__(self, recorder: Any, *, own_package: str = "", host: Callable[[], tuple] | None = None,
                 log: Any = None, clock: Callable[[], float] = time.perf_counter):
        self.recorder = recorder
        self.own_package = own_package
        self._host = host
        self.log = log
        self.clock = clock
        self.active = True
        self._lock = threading.Lock()
        self._wrapped: list[tuple[Any, Any, Any, str]] = []   # (元数据, 包装函数, 原函数, 钩子短名)
        self.handler_stats: dict[str, list] = {}              # "插件.函数" → [次数, 总毫秒, 最慢毫秒, 钩子短名]
        self.status: dict[str, Any] = {"available": None, "wrapped": 0, "by_hook": {}, "error": None}

    # ------------------------------------------------------------ 宿主入口
    def _hosts(self) -> tuple[dict[str, list], dict]:
        """→ ({钩子短名: [处理函数元数据…]}, star_map)。测试里 host() 可以只给 on_llm_request 的列表。"""
        if self._host is not None:
            h, star_map = self._host()
            return (({"on_llm_request": h} if isinstance(h, list) else dict(h)), star_map)
        from astrbot.core.star.star import star_map  # 非公开 API
        from astrbot.core.star.star_handler import EventType, star_handlers_registry
        by_hook: dict[str, list] = {
            "on_llm_request": star_handlers_registry.get_handlers_by_event_type(EventType.OnLLMRequestEvent)}
        for attr, short, _mode in HOOKS[1:]:
            et = getattr(EventType, attr, None)
            if et is None:
                continue
            try:
                by_hook[short] = star_handlers_registry.get_handlers_by_event_type(et)
            except Exception:
                continue   # 某个钩子取不到就跳过，别的照常
        return by_hook, star_map

    def _is_own(self, meta: Any) -> bool:
        path = str(getattr(meta, "handler_module_path", "") or "")
        return bool(self.own_package) and (path == self.own_package or path.startswith(self.own_package + "."))

    @staticmethod
    def _plugin_name(meta: Any, star_map: dict) -> str:
        path = str(getattr(meta, "handler_module_path", "") or "")
        try:
            md = star_map.get(path)
            name = getattr(md, "name", None)
            if name:
                return str(name)
        except Exception:
            pass
        return path.rsplit(".", 1)[0].rsplit(".", 1)[-1] or path

    # ------------------------------------------------------------ 包装 / 还原
    def ensure_wrapped(self) -> int:
        """给还没包的处理函数包上一层；每次请求都调一次（便宜，已包的直接跳过）。"""
        if not self.active:
            return 0
        try:
            by_hook, star_map = self._hosts()
        except Exception as exc:
            self.status.update(available=False, error=f"{type(exc).__name__}: {exc}")
            return 0
        n = 0
        for short, handlers in by_hook.items():
            for meta in list(handlers):
                try:
                    if self._is_own(meta):
                        continue
                    cur = meta.handler
                    if not isinstance(cur, functools.partial):
                        continue   # 没绑实例的（插件没启用）不动，启用后下次请求再包
                    raw = cur.func
                    if getattr(raw, "_sf_owner", None) is self:
                        continue
                    if getattr(raw, "_sf_wrapped", False):
                        raw = raw._sf_orig   # 旧实例包的（会话流重载过）：拆开重包
                    if not inspect.iscoroutinefunction(raw):
                        continue
                    wrapper = self._make(raw, meta, self._plugin_name(meta, star_map), short)
                    meta.handler = functools.partial(wrapper, *cur.args, **(cur.keywords or {}))
                    with self._lock:
                        self._wrapped.append((meta, wrapper, raw, short))
                    n += 1
                except Exception as exc:
                    self.status["error"] = f"{type(exc).__name__}: {exc}"
        self.status["available"] = True if (n or self._wrapped) else self.status["available"]
        with self._lock:
            self.status["wrapped"] = len(self._wrapped)
            by: dict[str, int] = {}
            for _, _, _, short in self._wrapped:
                by[short] = by.get(short, 0) + 1
            self.status["by_hook"] = by
        return n

    def restore(self) -> int:
        """停用 / 卸载时把包装拆掉，处理函数还原成原样。"""
        self.active = False
        n = 0
        with self._lock:
            items, self._wrapped = self._wrapped, []
        for meta, wrapper, raw, _short in items:
            try:
                cur = meta.handler
                if isinstance(cur, functools.partial) and cur.func is wrapper:
                    meta.handler = functools.partial(raw, *cur.args, **(cur.keywords or {}))
                    n += 1
            except Exception:
                pass
        self.status["wrapped"] = 0
        self.status["by_hook"] = {}
        return n

    def _make(self, raw: Callable, meta: Any, plugin: str, hook: str = "on_llm_request") -> Callable:
        handler_name = str(getattr(meta, "handler_name", "") or getattr(raw, "__name__", ""))
        try:
            priority = (getattr(meta, "extras_configs", None) or {}).get("priority")
        except Exception:
            priority = None
        owner = self
        mode = _MODE.get(hook)
        key = f"{plugin}.{handler_name}"

        @functools.wraps(raw)
        async def wrapper(*args, **kwargs):
            ev = tgt = before = None
            t0 = owner.clock()
            try:
                if owner.active:
                    ev, tgt = owner._find(args, kwargs, mode or "none")
                    if mode and ev is not None and (tgt is not None or mode == "result"):
                        before = owner._snap(mode, ev, tgt)
            except Exception:
                before = None
            try:
                return await raw(*args, **kwargs)
            finally:
                try:
                    ms = (owner.clock() - t0) * 1000.0
                    owner._stat(key, ms, hook)
                    if before is not None and owner.active:
                        owner._note(mode, ev, tgt, before, plugin, handler_name, priority, hook)
                    if ev is not None and owner.active:
                        report = getattr(owner.recorder, "add_handler_timing", None)
                        if callable(report):
                            report(ev, {"hook": hook, "plugin": plugin, "handler": handler_name, "ms": round(ms, 1)})
                except Exception:
                    pass   # 记录失败绝不能影响别人的处理函数

        wrapper._sf_wrapped = True
        wrapper._sf_owner = owner
        wrapper._sf_orig = raw
        return wrapper

    # ------------------------------------------------------------ 快照 / 记账
    @staticmethod
    def _find(args: tuple, kwargs: dict, mode: str = "request") -> tuple[Any, Any]:
        ev = tgt = None
        for a in list(args) + list(kwargs.values()):
            if ev is None and hasattr(a, "unified_msg_origin"):
                ev = a
            elif tgt is None and mode == "request" and hasattr(a, "system_prompt") and hasattr(a, "prompt"):
                tgt = a
            elif tgt is None and mode == "response" and hasattr(a, "completion_text"):
                tgt = a
        return ev, tgt

    @staticmethod
    def _snap(mode: str, ev: Any, tgt: Any) -> tuple:
        if mode == "request":
            req = tgt
            func_tool = getattr(req, "func_tool", None)
            names: list = []
            if func_tool is not None:
                try:
                    fn = getattr(func_tool, "names", None)
                    names = list(fn() if callable(fn) else (fn or []))
                except Exception:
                    names = []
            return (str(getattr(req, "system_prompt", "") or ""), str(getattr(req, "prompt", "") or ""),
                    list(getattr(req, "extra_user_content_parts", None) or []),
                    ex.ctx_fingerprint(getattr(req, "contexts", None)), names,
                    [str(u)[:80] + f"…({len(str(u))}字)" if len(str(u)) > 80 else str(u)
                     for u in (getattr(req, "image_urls", None) or [])],
                    str(getattr(req, "model", "") or ""))
        if mode == "response":
            return (str(getattr(tgt, "completion_text", "") or ""),)
        # result：发出前的消息链
        return (ex.result_sig(ev.get_result()),)

    def _stat(self, key: str, ms: float, hook: str = "on_llm_request") -> None:
        with self._lock:
            s = self.handler_stats.setdefault(key, [0, 0.0, 0.0, hook])
            s[0] += 1
            s[1] += ms
            s[2] = max(s[2], ms)

    def _note(self, mode: str, ev: Any, tgt: Any, before: tuple, plugin: str, handler: str,
              priority: Any, hook: str) -> None:
        if ev is None:
            return
        base = {"hook": hook, "plugin": plugin, "handler": handler, "priority": priority}
        after = self._snap(mode, ev, tgt)
        if mode == "request":
            sp0, pr0, parts0, ctx0, names0, imgs0, model0 = before
            sp1, pr1, parts1, ctx1, names1, imgs1, model1 = after
            injects: list[dict] = []
            if sp1 != sp0:
                injects += ex.diff_injections(sp0, sp1, "系统提示词", "system_prompt")
            if pr1 != pr0:
                injects += ex.diff_injections(pr0, pr1, "提问", "prompt")
            seen = {id(p) for p in parts0}
            for p in parts1:
                if id(p) in seen:
                    continue
                text = _part_text(p)
                if text.strip():
                    injects.append({"name": ex.inject_name(text), "text": text, "source": "extra_part"})
            edits: list[dict] = []
            d = ex.diff_contexts(ctx0, ctx1)
            if d:
                edits.append({"what": "历史", "text": ex.describe_contexts(d)})
            d = ex.diff_names(names0, names1)
            if d:
                edits.append({"what": "工具集", "text": ex.describe_names(d, "工具集")})
            if imgs0 != imgs1:
                edits.append({"what": "图片", "text": f"图片 {len(imgs0)} → {len(imgs1)} 张"
                              + ("\n" + "\n".join(imgs1[:5]) if imgs1 else "")})
            if model0 != model1:
                edits.append({"what": "模型", "text": f"模型 {model0 or '（空）'} → {model1 or '（空）'}"})
            if injects or edits:
                self.recorder.add_handler_note(ev, {**base, "injects": injects, "edits": edits})
        elif mode == "response":
            if after[0] != before[0]:
                self.recorder.add_handler_note(ev, {**base, "before": before[0], "after": after[0]})
        else:
            if after[0] != before[0]:
                cleared = not after[0]
                self.recorder.add_handler_note(ev, {**base, "before": ex.chain_text(before[0]),
                                                    "after": ex.chain_text(after[0]), "cleared": cleared})

    def stats(self) -> dict:
        with self._lock:
            return {
                "status": dict(self.status),
                "handlers": {k: {"hook": v[3], "count": v[0], "avg_ms": round(v[1] / v[0], 2) if v[0] else 0,
                                 "max_ms": round(v[2], 2)} for k, v in self.handler_stats.items()},
            }


# =================================================================== 疑似（字符串匹配）

class Guesser:
    """在已装插件 / 内核源码里找「<标签名>」字面量，给出候选；只是推测，页面标「疑似」。"""

    def __init__(self, roots: list[tuple[str, Path]] | None = None, *, ttl: float = 3600.0,
                 clock: Callable[[], float] = time.time):
        self.roots = list(roots or [])
        self.ttl = ttl
        self.clock = clock
        self._index: dict[str, dict[str, int]] | None = None
        self._built_at = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def default_roots(own_dir: Path) -> list[tuple[str, Path]]:
        roots: list[tuple[str, Path]] = []
        plugins_dir = own_dir.parent
        try:
            for d in sorted(plugins_dir.iterdir()):
                if d.is_dir() and d.resolve() != own_dir.resolve() and not d.name.startswith((".", "_")):
                    roots.append((d.name, d))
        except Exception:
            pass
        try:
            import astrbot
            core = Path(astrbot.__file__).resolve().parent
            for sub in ("core", "builtin_stars"):
                if (core / sub).is_dir():
                    roots.append(("AstrBot 内核", core / sub))
        except Exception:
            pass
        return roots

    def _build(self) -> dict[str, dict[str, int]]:
        index: dict[str, dict[str, int]] = {}
        for label, root in self.roots:
            for p in self._py_files(root):
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for tag in _TAG_LITERAL.findall(text):
                    by = index.setdefault(tag, {})
                    by[label] = by.get(label, 0) + 1
        return index

    @staticmethod
    def _py_files(root: Path):
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                for e in d.iterdir():
                    if e.is_dir():
                        if e.name not in _SKIP_DIRS and not e.name.startswith("."):
                            stack.append(e)
                    elif e.suffix == ".py":
                        yield e
            except Exception:
                continue

    def guess(self, name: str | None, limit: int = 3) -> list[str]:
        if not name or name.startswith(_NO_GUESS_PREFIX) or name == "inject":
            return []
        with self._lock:
            if self._index is None or self.clock() - self._built_at > self.ttl:
                self._index = self._build()
                self._built_at = self.clock()
            by = self._index.get(name) or {}
        return [k for k, _ in sorted(by.items(), key=lambda kv: -kv[1])][:limit]
