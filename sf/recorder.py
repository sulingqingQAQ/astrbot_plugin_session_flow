# -*- coding: utf-8 -*-
"""实时记录器：钩子 → 内存状态机 → 串行落库。

设计要点：
- 钩子里只做内存操作（+ 开运行时立即落底），DB 写经 submit 队列串行执行，绝不阻塞回复。
- “步（LLM 调用）”只在 agent_done 时由新增 assistant 消息推出（一条 assistant 一步）；
  on_llm_response 只记录每次响应的时间戳和 usage，agent_done 时按顺序对齐到步。
- 工具以 assistant 消息的 tool_calls 为准建 span，用钩子 start/end 按 call_id（缺失则按
  名字+顺序）配对；报错工具没有 tool_end，结束时间留空。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable

from . import ctxsize
from . import extract as ex
from .diag import mask_umo

MAX_TIMING_CALLS = 400   # 每轮最多记多少次处理函数调用（工具钩子一轮可能触发很多次）


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _now() -> float:
    return time.time()


# =================================================================== 内存状态

@dataclass
class ToolSpan:
    name: str
    args: Any = None
    call_id: str = ""
    start: float | None = None
    end: float | None = None
    result_text: str = ""
    is_error: bool = False
    result_cut: dict | None = None
    dur_ms: int | None = None
    step: int = 0
    order: int = 0  # 时间线次序


@dataclass
class ResponseTick:
    t: float
    usage: dict | None = None
    model: str | None = None


@dataclass
class LlmStep:
    idx: int
    thinks: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    call_ids: list[str] = field(default_factory=list)
    start: float | None = None
    end: float | None = None
    llm_ms: int | None = None
    response_tick: ResponseTick | None = None


@dataclass
class RunState:
    run_id: str
    umo: str
    cid: str
    platform_id: str
    message_type: str
    sender_id: str
    sender_name: str
    span_id: str
    seq_no: int
    sort_at: float
    received_at: float | None
    prep_at: float | None = None
    begin_at: float | None = None
    end_at: float | None = None
    msg_at: float | None = None
    user_spoken: str = ""
    user_images: int = 0
    sysprompt: str = ""
    sysprompt_hash: str = ""
    contexts_len: int | None = None
    tools_available: list[str] = field(default_factory=list)
    begin_anchor: int | None = None
    req_last_at: float | None = None                     # 最后一个请求钩子（会话流自己的）开始的时间
    timing_calls: list[dict] = field(default_factory=list)   # 别的插件处理函数的每次调用 {hook, plugin, handler, ms}
    injects: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)      # 其他字段（历史 / 工具集 / 图片 / 模型）被谁改了
    rewrites: list[dict] = field(default_factory=list)   # on_llm_response 里谁改写了模型回复
    first_texts: list[str] = field(default_factory=list)  # 最高优先级钩子拍快照时就已经在的文本
    steps: list[LlmStep] = field(default_factory=list)
    tool_spans: list[ToolSpan] = field(default_factory=list)
    response_ticks: list[ResponseTick] = field(default_factory=list)
    raw_tool_starts: list[dict] = field(default_factory=list)   # {t,name,args}
    raw_tool_ends: list[dict] = field(default_factory=list)     # {t,name,text,is_error,cut}
    mid_users: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    n_plugin_errors: int = 0
    n_model_errors: int = 0  # S4：模型失败也算一次报错（_fail_close 里加）
    provider_id: str | None = None
    model: str | None = None
    last_event_at: float = field(default_factory=_now)
    last_response_at: float | None = None
    current_tool: str = ""
    status: str = "running"
    closed: bool = False
    reply_text: str = ""
    error_text: str = ""
    done_t: float | None = None
    n_tools: int = 0
    n_tool_errors: int = 0


# =================================================================== 配置

@dataclass
class RecorderConfig:
    enable_record: bool = True
    exclude_umos: list[str] = field(default_factory=list)
    store_system_prompt: bool = True
    max_text_chars: int = 50000
    max_tool_result_chars: int = 20000
    stale_close_minutes: int = 30

    @classmethod
    def from_config(cls, cfg: Any, get: Callable[..., Any]) -> "RecorderConfig":
        return cls(
            enable_record=bool(get(cfg, "enable_record", True)),
            exclude_umos=list(get(cfg, "exclude_umos", []) or []),
            store_system_prompt=bool(get(cfg, "store_system_prompt", True)),
            max_text_chars=int(get(cfg, "max_text_chars", 50000)),
            max_tool_result_chars=int(get(cfg, "max_tool_result_chars", 20000)),
            stale_close_minutes=int(get(cfg, "stale_close_minutes", 30)),
        )

    def reload(self, cfg: Any, get: Callable[..., Any]) -> None:
        self.__dict__.update(self.from_config(cfg, get).__dict__)

    def excluded(self, umo: str) -> bool:
        return any(fnmatch.fnmatchcase(umo, pat) for pat in self.exclude_umos)


# =================================================================== 记录器

class Recorder:
    def __init__(self, store, names, cfg: RecorderConfig, *,
                 clock: Callable[[], float] = _now, log=None):
        self.store = store
        self.names = names
        self.cfg = cfg
        self.clock = clock
        self.log = log
        self._lock = threading.RLock()
        self.open_by_eid: dict[int, RunState] = {}
        self.open_by_umo: dict[str, str] = {}
        self.received_at: dict[int, float] = {}
        self.prep_at: dict[int, float] = {}
        # 最先拿到的 system_prompt / prompt（别的插件改之前），用来算“谁往提示词里塞了什么”
        self.first_req: dict[int, tuple[float, str, str, list[str]]] = {}
        # 被包装的 on_llm_request 处理函数各自造成的改动（谁改的）：eid → {t, notes:[…]}
        self.handler_notes: dict[int, dict] = {}
        # 运行还没建起来之前（等待钩子、请求钩子）别的插件处理函数的耗时：eid → {t, calls:[…]}
        self.pre_calls: dict[int, dict] = {}
        # 已结束（agent_done）、还没走到发送前最后一个钩子的运行：eid → {run_id, umo, reply, rewrites, t, ref}
        # 发送前那一步要拿它和“实际发出的内容”比；用弱引用核对事件，防 id 被回收复用串到别的运行
        self.closed_by_eid: dict[int, dict] = {}
        self._span_seq: dict[str, int] = {}
        self._submit: Callable[[Callable[[Any], None]], None] | None = None

    def set_submit(self, submit: Callable[[Callable[[Any], None]], None]) -> None:
        self._submit = submit

    def _enqueue(self, job: Callable[[Any], None]) -> None:
        if self._submit is not None:
            self._submit(job)
        else:
            job(self.store)  # 离线测试：直接同步跑

    def _log(self, level: str, msg: str) -> None:
        if self.log:
            try:
                getattr(self.log, level, self.log.info)("[session_flow] " + msg)
            except Exception:
                pass

    def _run_note(self, msg: str) -> None:
        """每轮运行的开始 / 结束记一行（只进诊断日志，不刷宿主控制台）。宿主 logger 没这个方法就不记。"""
        fn = getattr(self.log, "run_note", None)
        if callable(fn):
            try:
                fn(msg)
            except Exception:
                pass

    def _name(self, umo: str) -> str:
        try:
            return self.names.resolve(umo)
        except Exception:
            return umo

    # ---------------------------------------------------------- 事件字段
    @staticmethod
    def _event_fields(event: Any) -> dict:
        def g0(attr, default=None):
            try:
                v = getattr(event, attr)
                return default if v is None else v
            except Exception:
                return default
        umo = str(g0("unified_msg_origin", "") or "")
        platform_id = str(g0("platform_id") or (umo.split(":", 1)[0] if umo else ""))
        try:
            mtype = str(event.get_message_type())
        except Exception:
            mtype = ""
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        try:
            sender_name = str(event.get_sender_name() or "") or sender_id
        except Exception:
            sender_name = sender_id
        msg_at = None
        try:
            ts = getattr(getattr(event, "message_obj", None), "timestamp", None)
            if ts is not None:
                msg_at = float(ts)
        except Exception:
            msg_at = None
        return {
            "eid": id(event), "umo": umo, "platform_id": platform_id,
            "message_type": mtype, "sender_id": sender_id,
            "sender_name": sender_name, "msg_at": msg_at,
        }

    def _get_open(self, event: Any) -> RunState | None:
        # 只认同一事件（S1：同群别的事件不能顺着 umo 摸到正在跑的运行，
        # 否则一条指令回复的 decorating 会把跑工具的运行误关成失败）。
        # 新一轮打断旧一轮走 on_request_last 的显式逻辑，不走这里。
        f = self._event_fields(event)
        with self._lock:
            return self.open_by_eid.get(f["eid"])

    # ---------------------------------------------------------- api/active
    def active_runs(self) -> list[dict]:
        now = self.clock()
        out = []
        with self._lock:
            for st in self.open_by_eid.values():
                if st.closed:
                    continue
                out.append({
                    "run_id": st.run_id,
                    "umo": st.umo,
                    "display_name": self._name(st.umo),
                    "started_at": st.begin_at or st.prep_at or st.received_at or st.sort_at,
                    "last_event_at": st.last_event_at,
                    "idle_sec": max(0, int(now - st.last_event_at)),
                    "current_tool": st.current_tool,
                    "n_tools": len(st.tool_spans) if st.tool_spans else len(st.raw_tool_starts),
                })
        return out

    # ---------------------------------------------------------- 钩子
    def on_waiting(self, event: Any) -> None:
        try:
            eid = self._event_fields(event)["eid"]
            with self._lock:
                self.received_at[eid] = self.clock()
                self._sweep_temp()
        except Exception:
            self._log("exception", "on_waiting 记录失败")

    def on_request_first(self, event: Any, req: Any = None) -> None:
        try:
            f = self._event_fields(event)
            eid = f["eid"]
            with self._lock:
                self.prep_at[eid] = self.clock()
                if req is not None and self.cfg.enable_record and not self.cfg.excluded(f["umo"]):
                    extras = []
                    for p in (getattr(req, "extra_user_content_parts", None) or []):
                        try:
                            extras.append(ex.part_text(p) or "")
                        except Exception:
                            pass
                    self.first_req[eid] = (self.clock(), str(getattr(req, "system_prompt", "") or ""),
                                           str(getattr(req, "prompt", "") or ""), extras)
                self._sweep_temp()
        except Exception:
            self._log("exception", "on_request_first 记录失败")

    def add_handler_note(self, event: Any, note: dict) -> None:
        """Attributor 的包装在某个处理函数跑完后调：这个函数改了什么。按 note["hook"] 分流：
        on_llm_request（默认）→ 请求里的注入 / 其他字段；on_llm_response → 回复改写；on_decorating_result → 发出前改写。"""
        try:
            eid = id(event)
            hook = note.get("hook") or "on_llm_request"
            with self._lock:
                if hook == "on_llm_response":
                    st = self.open_by_eid.get(eid)
                    if st is not None and not st.closed:
                        st.rewrites.append(note)
                    return
                if hook == "on_decorating_result":
                    p = self.closed_by_eid.get(eid)
                    if p is not None and self._same_event(p, event):
                        p["rewrites"].append(note)
                    return
                if eid not in self.first_req:
                    return   # 这个事件没在记录（关了 / 排除了），别攒
                slot = self.handler_notes.setdefault(eid, {"t": self.clock(), "notes": []})
                slot["notes"].append(note)
        except Exception:
            self._log("exception", "add_handler_note 失败")

    def add_handler_timing(self, event: Any, call: dict) -> None:
        """Attributor 的包装每调完一次别的插件的处理函数就报一次耗时（不管有没有改东西）。
        运行还没建起来 → 先放进 pre_calls；运行在跑 → 记在运行上；运行已结束、等着发出 → 记在待补的那一项上。"""
        try:
            if not self.cfg.enable_record:
                return
            eid = id(event)
            with self._lock:
                st = self.open_by_eid.get(eid)
                if st is not None and not st.closed:
                    if len(st.timing_calls) < MAX_TIMING_CALLS:
                        st.timing_calls.append(call)
                    return
                p = self.closed_by_eid.get(eid)
                if p is not None and self._same_event(p, event):
                    calls = p.setdefault("calls", [])
                    if len(calls) < MAX_TIMING_CALLS:
                        calls.append(call)
                    return
                if call.get("hook") in ("on_waiting_llm_request", "on_llm_request"):
                    slot = self.pre_calls.setdefault(eid, {"t": self.clock(), "calls": []})
                    if len(slot["calls"]) < MAX_TIMING_CALLS:
                        slot["calls"].append(call)
        except Exception:
            self._log("exception", "add_handler_timing 失败")

    @staticmethod
    def agg_calls(calls: list[dict] | None, limit: int = 60) -> list[dict]:
        """同一个处理函数在同一个钩子上的多次调用合成一行：次数 / 总毫秒 / 最慢毫秒。按总毫秒从大到小。"""
        agg: dict[tuple, dict] = {}
        for c in calls or []:
            key = (c.get("hook"), c.get("plugin"), c.get("handler"))
            a = agg.get(key)
            ms = float(c.get("ms") or 0)
            if a is None:
                agg[key] = {"hook": key[0], "plugin": key[1], "handler": key[2], "n": 1,
                            "total_ms": ms, "max_ms": ms}
            else:
                a["n"] += 1
                a["total_ms"] += ms
                a["max_ms"] = max(a["max_ms"], ms)
        out = sorted(agg.values(), key=lambda x: -x["total_ms"])[:limit]
        for a in out:
            a["total_ms"] = round(a["total_ms"], 1)
            a["max_ms"] = round(a["max_ms"], 1)
        return out

    @staticmethod
    def _same_event(pending: dict, event: Any) -> bool:
        ref = pending.get("ref")
        if ref is None:
            return True
        try:
            return ref() is event
        except Exception:
            return False

    def on_request_last(self, event: Any, req: Any) -> RunState | None:
        try:
            f = self._event_fields(event)
            if not self.cfg.enable_record or self.cfg.excluded(f["umo"]):
                self.pre_calls.pop(f["eid"], None)
                return None
            with self._lock:
                if f["eid"] in self.open_by_eid:
                    return self.open_by_eid[f["eid"]]
                cid = ""
                conv = getattr(req, "conversation", None)
                if conv is not None:
                    cid = str(getattr(conv, "cid", "") or "")
                span_id = self._span_id(event)
                n = self._span_seq.get(span_id, 0) + 1
                self._span_seq[span_id] = n
                run_id = f"{span_id}#{n}"
                received = self.received_at.get(f["eid"])
                prep = self.prep_at.get(f["eid"])
                st = RunState(
                    run_id=run_id, umo=f["umo"], cid=cid,
                    platform_id=f["platform_id"], message_type=f["message_type"],
                    sender_id=f["sender_id"], sender_name=f["sender_name"],
                    span_id=span_id, seq_no=n,
                    sort_at=f["msg_at"] or received or self.clock(),
                    received_at=received, prep_at=prep, msg_at=f["msg_at"],
                )
                st.req_last_at = self.clock()
                pre = self.pre_calls.pop(f["eid"], None)
                if pre:
                    st.timing_calls = list(pre["calls"])
                st.user_spoken = getattr(req, "prompt", "") or ""
                sp = getattr(req, "system_prompt", "") or ""
                st.sysprompt = sp
                first = self.first_req.pop(f["eid"], None)
                slot = self.handler_notes.pop(f["eid"], None)
                if first is not None:
                    t_inj = st.prep_at or st.received_at or st.sort_at
                    st.first_texts = [first[2]] + list(first[3])
                    final_extras = []
                    for p in (getattr(req, "extra_user_content_parts", None) or []):
                        try:
                            final_extras.append(ex.part_text(p) or "")
                        except Exception:
                            pass
                    exact: set = set()
                    for note in (slot or {}).get("notes", []):
                        for ed in note.get("edits") or []:
                            st.edits.append({"t": t_inj, "what": ed["what"], "text": ed["text"],
                                             "plugin": note["plugin"], "handler": note["handler"],
                                             "priority": note.get("priority")})
                        for inj in note.get("injects") or []:
                            exact.add((inj["name"], inj["text"]))
                            src = inj.get("source")
                            where = (sp if src == "system_prompt" else st.user_spoken if src == "prompt"
                                     else "\n".join(final_extras))
                            st.injects.append({
                                "t": t_inj, **inj, "plugin": note["plugin"], "handler": note["handler"],
                                "priority": note.get("priority"), "attrib": "exact",
                                "gone": inj["text"] not in where,   # 后来又被别的处理函数改掉了
                            })
                    for inj in (ex.diff_injections(first[1], sp, "系统提示词", "system_prompt")
                                + ex.diff_injections(first[2], st.user_spoken, "提问", "prompt")):
                        if (inj["name"], inj["text"]) in exact:
                            continue
                        # 已经有人认领的片段从这一块里减掉，只留没人认领的；减完没剩的就是重复，不记
                        mine = [x["text"] for x in st.injects
                                if x.get("attrib") == "exact" and x.get("source") == inj.get("source")]
                        rest = ex.subtract_texts(inj["text"], mine)
                        if not rest.strip():
                            continue
                        if rest != inj["text"]:
                            inj = {**inj, "text": rest.strip("\n")}
                        st.injects.append({"t": t_inj, **inj, "attrib": "unattributed"})
                if sp:
                    st.sysprompt_hash = _sha(sp)
                try:
                    st.contexts_len = len(getattr(req, "contexts", None) or [])
                except Exception:
                    st.contexts_len = None
                func_tool = getattr(req, "func_tool", None)
                if func_tool is not None:
                    try:
                        # 真实 func_tool.names 是方法（S2：旧代码当属性取被吞掉恒 []）
                        names = getattr(func_tool, "names", None)
                        st.tools_available = list(names() if callable(names) else (names or []))
                    except Exception:
                        st.tools_available = []
                ctx_in = self._ctx_input(req, st)
                interrupted_ids: list[str] = []
                old_rid = self.open_by_umo.get(f["umo"])
                if old_rid and old_rid != run_id:
                    for eid0, ost in list(self.open_by_eid.items()):
                        if ost.run_id == old_rid and not ost.closed:
                            ost.closed = True
                            ost.status = "interrupted"
                            ost.end_at = self.clock()
                            interrupted_ids.append(old_rid)
                            self.open_by_eid.pop(eid0, None)
                            self._span_seq.pop(ost.span_id, None)
                            break
                    self.open_by_umo.pop(f["umo"], None)
                self.open_by_eid[f["eid"]] = st
                self.open_by_umo[f["umo"]] = run_id
                self._sweep_temp()
                self._persist_open(st, interrupted_ids, ctx_in)
                return st
        except Exception:
            self._log("exception", "on_request_last 记录失败")
            return None

    def on_agent_begin(self, event: Any, run_context: Any) -> None:
        try:
            st = self._get_open(event)
            if st is None:
                return
            t = self.clock()
            with self._lock:
                st.begin_at = t
                st.last_event_at = t
                messages = list(getattr(run_context, "messages", None) or [])
                anchor = None
                for i in range(len(messages) - 1, -1, -1):
                    d = ex.model_obj(messages[i])
                    if isinstance(d, dict) and d.get("role") == "user":
                        anchor = i
                        break
                st.begin_anchor = anchor
                if anchor is not None:
                    um = ex.model_obj(messages[anchor])
                    content = um.get("content") if isinstance(um, dict) else messages[anchor]
                    spoken, injects, images = ex.split_user_content(content)
                    st.user_images = images
                    if spoken.strip():
                        st.user_spoken = spoken
                    for inj in injects:
                        earlier = any(inj["text"] in x for x in st.first_texts)
                        st.injects.append({"t": t, "name": inj["name"], "text": inj["text"],
                                           "attrib": "earlier" if earlier else "unattributed"})
        except Exception:
            self._log("exception", "on_agent_begin 记录失败")

    def on_tool_start(self, event: Any, tool: Any, tool_args: Any) -> None:
        try:
            st = self._get_open(event)
            if st is None:
                return
            t = self.clock()
            with self._lock:
                name = str(getattr(tool, "name", "") or "")
                st.raw_tool_starts.append({"t": t, "name": name, "args": tool_args})
                st.current_tool = name
                st.last_event_at = t
        except Exception:
            self._log("exception", "on_tool_start 记录失败")

    def on_tool_end(self, event: Any, tool: Any, tool_args: Any, tool_result: Any) -> None:
        try:
            st = self._get_open(event)
            if st is None:
                return
            t = self.clock()
            name = str(getattr(tool, "name", "") or "")
            text, is_error, cut = ex.tool_result_text(tool_result, self.cfg.max_tool_result_chars)
            with self._lock:
                st.raw_tool_ends.append({
                    "t": t, "name": name, "text": text,
                    "is_error": bool(is_error or ex.is_error_result_text(text)), "cut": cut,
                })
                st.current_tool = ""
                st.last_event_at = t
        except Exception:
            self._log("exception", "on_tool_end 记录失败")

    def on_llm_response(self, event: Any, resp: Any) -> None:
        try:
            st = self._get_open(event)
            if st is None:
                return
            t = self.clock()
            usage, model = None, None
            u = getattr(resp, "usage", None)
            if u is not None:
                usage = u if isinstance(u, dict) else ex.model_obj(u)
            model = getattr(resp, "model", None)
            with self._lock:
                st.response_ticks.append(ResponseTick(t=t, usage=usage, model=str(model) if model else None))
                st.last_response_at = t
                st.last_event_at = t
                if model:
                    st.model = st.model or str(model)
        except Exception:
            self._log("exception", "on_llm_response 记录失败")

    def on_agent_done(self, event: Any, run_context: Any, response: Any) -> None:
        try:
            st = self._get_open(event)
            if st is None or st.closed:
                return
            t = self.clock()
            with self._lock:
                messages = list(getattr(run_context, "messages", None) or [])
                new_msgs = self._slice_new_messages(st, messages)
                self._build_from_messages(st, new_msgs, response, t)
                self._align_ticks(st)
                self._match_tool_times(st)
                self._compute_step_times(st, t)
                st.done_t = t
                st.status = "done"
                st.closed = True
                st.end_at = t
                self._close(st)
                try:
                    ref = weakref.ref(event)
                except TypeError:
                    ref = None
                self.closed_by_eid[id(event)] = {
                    "run_id": st.run_id, "umo": st.umo, "reply": st.reply_text,
                    "rewrites": list(st.rewrites), "t": t, "ref": ref, "calls": []}
        except Exception:
            self._log("exception", "on_agent_done 记录失败")

    def on_sent(self, event: Any) -> None:
        """发送前最后一个钩子：实际要发出的内容和模型原话不一样，才在这一轮后面补一条“实际发出”。

        一样（忽略空白、@ / 引用 / 表情）就什么都不记，不重复。宿主自己的回复前缀 / 分段 / 语音 / 文转图在
        插件钩子之后，不在这里比。流式中间帧（STREAMING_RESULT）不处理，等 STREAMING_FINISH。
        """
        try:
            eid = id(event)
            with self._lock:
                p = self.closed_by_eid.get(eid)
                if p is None:
                    return
                if not self._same_event(p, event):
                    self.closed_by_eid.pop(eid, None)   # id 被回收复用，不是同一个事件
                    return
            try:
                result = event.get_result()
            except Exception:
                result = None
            ctype = getattr(result, "result_content_type", None)
            if str(getattr(ctype, "name", ctype) or "") == "STREAMING_RESULT":
                return
            with self._lock:
                self.closed_by_eid.pop(eid, None)
            t = self.clock()
            run_id = p["run_id"]
            # 回复之后的耗时（agent 结束 → 走到发送前最后一个钩子，以及这段时间里别的插件处理函数的耗时）总是补一条
            timing = {"run_id": run_id, "t": t, "kind": "timing", "step": 0, "name": "timing", "text": "",
                      "data": {"v": 1, "phase": "after", "agent_done_at": p["t"], "decorating_at": t,
                               "calls": self.agg_calls(p.get("calls"))}}
            sig = ex.result_sig(result)
            plain, media = ex.sent_signature(sig)
            if plain == ex.norm_text(p["reply"]) and not media:
                def job0(store):
                    store.insert_events([{**timing, "seq": store.max_event_seq(run_id) + 1}])
                self._enqueue(job0)
                return
            rewrites = []
            for n in p["rewrites"][:8]:
                item = {"hook": n.get("hook"), "plugin": n.get("plugin"), "handler": n.get("handler"),
                        "before": (n.get("before") or "")[:1000], "after": (n.get("after") or "")[:1000]}
                if n.get("priority") is not None:
                    item["priority"] = n["priority"]
                if n.get("cleared"):
                    item["cleared"] = True
                rewrites.append(item)
            data = {"attrib": "exact" if rewrites else "unattributed", "rewrites": rewrites}
            text = ex.chain_text(sig) or "（空：被清除，没发出）"
            ev = {"run_id": run_id, "t": t, "kind": "sent", "step": 0, "name": "实际发出",
                  "text": self._clip(text), "data": data}

            def job(store):
                base = store.max_event_seq(run_id)
                store.insert_events([{**timing, "seq": base + 1}, {**ev, "seq": base + 2}])
            self._enqueue(job)
        except Exception:
            self._log("exception", "on_sent 记录失败")

    def on_decorating(self, event: Any) -> bool:
        """agent_done 没来却到了结果阶段：判断是否失败关闭。"""
        try:
            st = self._get_open(event)
            if st is None or st.closed:
                return False
            result = None
            try:
                result = event.get_result()
            except Exception:
                result = None
            if result is None:
                return False
            ctype = getattr(result, "result_content_type", None)
            cname = str(getattr(ctype, "name", ctype) or "")
            try:
                is_llm = bool(result.is_llm_result())
            except Exception:
                is_llm = cname == "LLM_RESULT"
            # 流式中间帧不算结束；正常 LLM_RESULT 说明 agent_done 应该/已经来过
            if cname == "STREAMING_RESULT":
                return False
            if is_llm or cname == "LLM_RESULT":
                return False
            # STREAMING_FINISH 但 is_llm_result()=False、GENERAL_RESULT 等 → 模型失败
            return self._fail_close(st, result)
        except Exception:
            self._log("exception", "on_decorating 记录失败")
            return False

    def on_plugin_error(self, event: Any | None, plugin_name: str, handler_name: str,
                        error: Any, tb: str) -> None:
        try:
            t = self.clock()
            name = f"{plugin_name}/{handler_name}" if (plugin_name or handler_name) else (plugin_name or "plugin")
            text = str(error)[:500]
            st = self._get_open(event) if event is not None else None
            with self._lock:
                if st is not None and not st.closed:
                    st.errors.append({"t": t, "name": name, "text": text})
                    st.n_plugin_errors += 1
                    st.last_event_at = t
                else:
                    self._persist_note_error(event, t, name, text)
        except Exception:
            self._log("exception", "on_plugin_error 记录失败")

    # ---------------------------------------------------------- 关闭
    def _fail_close(self, st: RunState, result: Any) -> bool:
        try:
            text = ""
            if hasattr(result, "get_plain_text"):
                text = result.get_plain_text() or ""
            text = (text or "模型调用失败")[: self.cfg.max_text_chars]
        except Exception:
            text = "模型调用失败"
        t = self.clock()
        with self._lock:
            st.errors.append({"t": t, "name": "llm", "text": text})
            st.n_model_errors += 1
            # 失败前可能已经跑了部分步/工具
            self._align_ticks(st)
            self._match_tool_times(st)
            self._compute_step_times(st, t)
            st.done_t = t
            st.error_text = text
            st.status = "error"
            st.closed = True
            st.end_at = t
            self._close(st)
        return True

    # ---------------------------------------------------------- 消息 → 步/工具
    def _slice_new_messages(self, st: RunState, messages: list[Any]) -> list[Any]:
        anchor = st.begin_anchor
        if anchor is not None and 0 <= anchor < len(messages):
            return list(messages[anchor + 1:])
        # 内容兜底
        for i in range(len(messages) - 1, -1, -1):
            d = ex.model_obj(messages[i])
            if isinstance(d, dict) and d.get("role") == "user":
                spoken, _, _ = ex.split_user_content(d.get("content"))
                if spoken.strip()[:120] and spoken.strip()[:120] == st.user_spoken.strip()[:120]:
                    st.begin_anchor = i
                    return list(messages[i + 1:])
        # 最后一条 user 之后
        for i in range(len(messages) - 1, -1, -1):
            d = ex.model_obj(messages[i])
            if isinstance(d, dict) and d.get("role") == "user":
                st.begin_anchor = i
                return list(messages[i + 1:])
        return []

    def _build_from_messages(self, st: RunState, new_msgs: list[Any], response: Any, done_t: float) -> None:
        reply = ""
        for m in new_msgs:
            d = ex.model_obj(m)
            if not isinstance(d, dict):
                continue
            role, content = d.get("role"), d.get("content")
            if role == "assistant":
                step = LlmStep(idx=len(st.steps) + 1)
                for seg in ex.assistant_segments(content):
                    (step.thinks if seg["kind"] == "think" else step.texts).append(seg["text"])
                for tc in ex.tool_calls_of(d):
                    span = ToolSpan(
                        name=tc["name"], args=tc["arguments"],
                        call_id=tc["id"] or "", step=step.idx,
                    )
                    step.call_ids.append(span.call_id)
                    st.tool_spans.append(span)
                st.steps.append(step)
            elif role == "tool":
                call_id = str(d.get("tool_call_id") or "")
                ctext = content if isinstance(content, str) else json.dumps(
                    ex.model_obj(content), ensure_ascii=False, default=str)
                is_err = ex.is_error_result_text(ctext or "")
                text, cut = ex.truncate(ctext or "", self.cfg.max_tool_result_chars)
                span = self._find_span_for_result(st, call_id)
                span.result_text = text
                span.result_cut = cut
                span.is_error = is_err
            elif role == "user":
                spoken, injects, images = ex.split_user_content(content)
                st.mid_users.append({"t": done_t, "text": spoken, "injects": injects, "images": images})
            elif role == "system":
                note = content if isinstance(content, str) else json.dumps(
                    ex.model_obj(content), ensure_ascii=False, default=str)
                if note:
                    st.notes.append({"t": done_t, "text": note})
        for m in reversed(new_msgs):
            d = ex.model_obj(m)
            if isinstance(d, dict) and d.get("role") == "assistant":
                texts = [s["text"] for s in ex.assistant_segments(d.get("content")) if s["kind"] == "text"]
                if texts:
                    reply = texts[-1]
                    break
        if not reply and response is not None:
            try:
                reply = getattr(response, "completion_text", "") or ""
            except Exception:
                reply = ""
        st.reply_text = reply

    def _find_span_for_result(self, st: RunState, call_id: str) -> ToolSpan:
        if call_id:
            for s in st.tool_spans:
                if s.call_id == call_id and not s.result_text:
                    return s
        # 找第一个还没结果的 span
        for s in st.tool_spans:
            if not s.result_text and s.end is None:
                if call_id:
                    s.call_id = call_id
                return s
        span = ToolSpan(name="", step=max(1, len(st.steps)))
        st.tool_spans.append(span)
        return span

    def _align_ticks(self, st: RunState) -> None:
        """on_llm_response 打点按顺序对齐到 assistant 步。"""
        ticks = st.response_ticks
        for i, step in enumerate(st.steps):
            if i < len(ticks):
                step.response_tick = ticks[i]
                if ticks[i].model and not st.model:
                    st.model = ticks[i].model

    def _match_tool_times(self, st: RunState) -> None:
        """raw_tool_starts/ends（钩子）按 call 顺序对齐到 tool_spans（消息）。

        先按 call_id 不可得（钩子不给 id），只能按名字+顺序：
        第 k 个同名 start 配第 k 个同名 span；end 同理。
        """
        starts_by_name: dict[str, list[dict]] = {}
        for r in st.raw_tool_starts:
            starts_by_name.setdefault(r["name"], []).append(r)
        used_start: dict[str, int] = {}
        for s in st.tool_spans:
            lst = starts_by_name.get(s.name, [])
            i = used_start.get(s.name, 0)
            if i < len(lst):
                r = lst[i]
                used_start[s.name] = i + 1
                if s.start is None:
                    s.start = r["t"]
                if (s.args in (None, "", "{}")) and r.get("args") is not None:
                    s.args = r["args"]
        ends_by_name: dict[str, list[dict]] = {}
        for r in st.raw_tool_ends:
            ends_by_name.setdefault(r["name"], []).append(r)
        used_end: dict[str, int] = {}
        for s in st.tool_spans:
            if s.end is not None:
                continue  # 已有结束时间（极少见）则不占用 hook 的 end
            lst = ends_by_name.get(s.name, [])
            j = used_end.get(s.name, 0)
            if j < len(lst):
                r = lst[j]
                used_end[s.name] = j + 1
                s.end = r["t"]
                if not s.result_text:
                    s.result_text = r["text"]
                    s.result_cut = r["cut"]
                    s.is_error = r["is_error"]
        # 有 start 没配对上 span 的（极少见，消息丢了）：补成报错 span
        matched_starts = sum(used_end.values())  # noqa: F841（保留语义清晰）
        accounted = set()
        for name, lst in starts_by_name.items():
            used = used_start.get(name, 0)
            for r in lst[used:]:
                accounted.add(id(r))
                st.tool_spans.append(ToolSpan(
                    name=name, args=r.get("args"), start=r["t"],
                    result_text="工具调用未返回结果", is_error=True,
                    step=max(1, len(st.steps)),
                ))

    def _compute_step_times(self, st: RunState, close_t: float) -> None:
        """步起止与工具耗时：一步从上一步结束（第一步从 agent 开始）起，到它发起的第一个工具开始 / 它的响应打点为止。"""
        prev_end = st.begin_at or st.prep_at or st.sort_at
        for i, step in enumerate(st.steps):
            step.start = prev_end
            spans = [s for s in st.tool_spans if s.step == step.idx]
            tick_t = step.response_tick.t if step.response_tick else None
            if spans:
                starts = [s.start for s in spans if s.start is not None]
                step.end = min(starts) if starts else tick_t
            else:
                step.end = tick_t
            if step.end is None:
                step.end = close_t
            if step.start is not None and step.end >= step.start:
                step.llm_ms = int((step.end - step.start) * 1000)
            for s in spans:
                if s.start is None:
                    s.start = step.end
                if s.end is not None and s.start is not None and s.end >= s.start:
                    s.dur_ms = int((s.end - s.start) * 1000)
                prev_end = s.end or s.start or prev_end
            if not spans:
                prev_end = step.end
        st.n_tools = len(st.tool_spans)
        st.n_tool_errors = sum(1 for s in st.tool_spans if s.is_error)

    # ---------------------------------------------------------- 落库
    def _ctx_input(self, req: Any, st: RunState) -> dict | None:
        """给“上下文体积构成”准备原料：钩子里只拷一份列表引用和几段字符串，真正数字数放到写库线程里做。"""
        try:
            func_tool = getattr(req, "func_tool", None)
            if func_tool is None:
                tools_text, n_tools = "", 0
            else:
                tools_text, n_tools = ctxsize.tools_json(func_tool), len(st.tools_available)
            extras = []
            for p in (getattr(req, "extra_user_content_parts", None) or []):
                try:
                    extras.append(ex.part_text(p) or "")
                except Exception:
                    pass
            return {"contexts": list(getattr(req, "contexts", None) or []), "system_prompt": st.sysprompt or "",
                    "prompt": st.user_spoken or "", "extras": extras, "tools_text": tools_text,
                    "n_tools": n_tools, "image_urls": len(getattr(req, "image_urls", None) or [])}
        except Exception:
            self._log("exception", "准备体积构成原料失败")
            return None

    def _persist_open(self, st: RunState, interrupted_ids: list[str], ctx_in: dict | None = None) -> None:
        self._run_note(
            f"运行开始 {st.run_id} · {mask_umo(st.umo)}"
            + (f" · 同会话新一轮打断了旧运行 {', '.join(interrupted_ids)}" if interrupted_ids else ""))

        def job(store):
            store.touch_umo(st.umo, st.platform_id, st.message_type, self._name(st.umo))
            if interrupted_ids:
                store.mark_interrupted(interrupted_ids)
            if st.sysprompt and st.sysprompt_hash and self.cfg.store_system_prompt:
                store.put_blob(st.sysprompt_hash, st.sysprompt)
            store.insert_run(self._run_fields(st, status="running"))
            user_text, user_cut = ex.truncate(st.user_spoken or "", self.cfg.max_text_chars)
            user_data: dict | None = None
            if st.user_images:
                user_data = {"images": st.user_images}
            if user_cut:
                user_data = {**(user_data or {}), **user_cut}
            store.insert_events([{
                "run_id": st.run_id, "seq": 1, "t": st.sort_at, "kind": "user",
                "step": 0, "text": user_text, "data": user_data,
            }])
            if ctx_in is not None:
                try:
                    data = ctxsize.measure(ctx_in["contexts"], system_prompt=ctx_in["system_prompt"],
                                           prompt=ctx_in["prompt"], extras=ctx_in["extras"],
                                           tools_text=ctx_in["tools_text"], n_tools=ctx_in["n_tools"],
                                           image_urls=ctx_in["image_urls"])
                except Exception as exc:   # 数不出来就记一条说明，页面显示“没数到”
                    data = {"v": ctxsize.VERSION, "error": f"{type(exc).__name__}: {exc}"}
                store.insert_events([{"run_id": st.run_id, "seq": 2, "t": self.clock(), "kind": "ctx",
                                      "step": 0, "name": "ctx", "text": "", "data": data}])
        self._enqueue(job)

    def _close(self, st: RunState) -> None:
        for eid, cand in list(self.open_by_eid.items()):
            if cand is st:
                self.open_by_eid.pop(eid, None)
        if self.open_by_umo.get(st.umo) == st.run_id:
            self.open_by_umo.pop(st.umo, None)
        # S9：运行结束了，span 计数清掉，不然长期运行一直涨
        # （trace span_id 每条消息唯一，不会重用；eid 兜底的极端重用最多丢一轮，不坏库）
        self._span_seq.pop(st.span_id, None)
        try:
            dur = max(0.0, (st.end_at or self.clock()) - (st.begin_at or st.sort_at or self.clock()))
        except Exception:
            dur = 0.0
        self._run_note(
            f"运行结束 {st.run_id} · {mask_umo(st.umo)} · 状态 {st.status} · "
            f"步 {len(st.steps)} · 工具 {len(st.tool_spans)} · 报错 {len(st.errors)} · 用时 {dur:.1f}s")
        self._enqueue(lambda store: self._flush_closed(store, st))

    def _flush_closed(self, store, st: RunState) -> None:
        try:
            start = store.max_event_seq(st.run_id) + 1
        except Exception:
            start = 2
        store.insert_events(self._build_events(st, start))
        fields = self._run_fields(st, status=st.status)
        fields["end_at"] = st.end_at
        if st.status == "error":
            fields["error_text"] = st.error_text or "模型调用失败"
        store.update_run(st.run_id, fields)
        store.recompute_umo(st.umo)

    def _clip(self, text: str | None) -> str:
        text = text or ""
        if len(text) <= self.cfg.max_text_chars:
            return text
        return text[: self.cfg.max_text_chars] + f"…（已截断，原长 {len(text)} 字）"

    def _build_events(self, st: RunState, seq_start: int = 2) -> list[dict]:
        """seq 默认从 2 起（1 是 open 时的 user 事件；写库时接在已有事件后面，见 _flush_closed）。"""
        evs: list[dict] = []
        seq = seq_start

        def add(**kw):
            nonlocal seq
            kw["run_id"] = st.run_id
            kw["seq"] = seq
            evs.append(kw)
            seq += 1

        seen_inj: set = set()
        for inj in st.injects:
            key = (inj["name"], inj["text"])
            if key in seen_inj:  # 提问里追加的标签块，在 agent_begin 的消息里会再出现一次，只留前面带来源的
                continue
            seen_inj.add(key)
            meta = {k: inj[k] for k in ("source", "removed", "plugin", "handler", "attrib") if inj.get(k)}
            if inj.get("priority") is not None:
                meta["priority"] = inj["priority"]
            if inj.get("gone"):
                meta["gone"] = True
            add(t=inj["t"], kind="inject", step=0, name=inj["name"],
                text=self._clip(inj["text"]), **({"data": meta} if meta else {}))

        for ed in st.edits:
            meta = {"attrib": "exact", "what": ed["what"], "plugin": ed["plugin"], "handler": ed["handler"]}
            if ed.get("priority") is not None:
                meta["priority"] = ed["priority"]
            add(t=ed["t"], kind="edit", step=0, name=ed["what"], text=self._clip(ed["text"]), data=meta)

        # 时间线：think/text 取步 end；工具 call 取 start、result 取 end
        line: list[tuple[float, int, str, dict]] = []
        for step in st.steps:
            t0 = step.end or st.done_t or st.sort_at or 0
            for think in step.thinks:
                line.append((t0, step.idx, "think", {"text": think, "llm_ms": step.llm_ms}))
            for text in step.texts:
                line.append((t0, step.idx, "text", {"text": text, "llm_ms": step.llm_ms}))
        for order, s in enumerate(st.tool_spans):
            line.append(((s.start or st.done_t or 0), s.step, "tool_call", {"span": s, "order": order}))
        line.sort(key=lambda x: (x[0], 0 if x[2] != "tool_call" else 1))

        for t0, step_no, kind, payload in line:
            if kind in ("think", "text"):
                add(t=t0, kind=kind, step=step_no,
                    text=self._clip(payload["text"]),
                    data={"llm_ms": payload["llm_ms"]} if payload["llm_ms"] is not None else None)
            else:
                s: ToolSpan = payload["span"]
                add(t=s.start, kind="tool_call", step=step_no, name=s.name,
                    call_id=s.call_id or None, text=ex.pretty_json_args(s.args), dur_ms=s.dur_ms)
                if s.result_text:
                    text, cut = ex.truncate(s.result_text, self.cfg.max_tool_result_chars)
                    add(t=s.end or s.start, kind="tool_result", step=step_no, name=s.name,
                        call_id=s.call_id or None, text=text,
                        is_error=1 if s.is_error else 0, dur_ms=s.dur_ms, data=cut)
        for mu in st.mid_users:
            add(t=mu["t"], kind="user", step=0, text=self._clip(mu["text"]),
                data={"mid_run": True, "images": mu.get("images", 0)})
            for inj in mu.get("injects", []):
                add(t=mu["t"], kind="inject", step=0, name=inj["name"],
                    text=self._clip(inj["text"]))
        for note in st.notes:
            add(t=note["t"], kind="note", step=0, text=self._clip(note["text"]))
        for er in st.errors:
            add(t=er["t"], kind="error", step=0, name=er["name"],
                text=self._clip(er["text"]), is_error=1)
        add(t=st.req_last_at or st.sort_at, kind="timing", step=0, name="timing", text="",
            data=self._timing_data(st))
        return evs

    def _timing_data(self, st: RunState) -> dict:
        """每轮的时间点和别的插件处理函数的耗时。页面用它画分段条（见 sf/timing.py）。"""
        tool_ms = sum(s.dur_ms for s in st.tool_spans if s.dur_ms)
        return {"v": 1, "phase": "run", "msg_at": st.msg_at, "received_at": st.received_at,
                "prep_at": st.prep_at, "req_last_at": st.req_last_at, "begin_at": st.begin_at,
                "end_at": st.end_at, "tool_ms": int(tool_ms), "calls": self.agg_calls(st.timing_calls)}

    def _run_fields(self, st: RunState, *, status: str) -> dict:
        tick = st.steps[-1].response_tick if st.steps else None
        return {
            "run_id": st.run_id, "umo": st.umo, "cid": st.cid,
            "source": "live", "status": status,
            "sort_at": st.sort_at, "ord": st.seq_no, "time_source": "hook",
            "sender_id": st.sender_id, "sender_name": st.sender_name,
            "msg_at": st.msg_at, "received_at": st.received_at, "prep_at": st.prep_at,
            "begin_at": st.begin_at, "end_at": st.end_at,
            "provider_id": st.provider_id, "model": st.model or (tick.model if tick else None),
            "user_text": self._clip(st.user_spoken),
            "reply_text": self._clip(st.reply_text),
            "error_text": st.error_text if status == "error" else None,
            "sysprompt_hash": st.sysprompt_hash if (st.sysprompt_hash and self.cfg.store_system_prompt) else None,
            "contexts_len": st.contexts_len,
            "tools_available": json.dumps(st.tools_available, ensure_ascii=False),
            "n_steps": len(st.steps),
            "n_tools": st.n_tools if st.closed else len(st.raw_tool_starts),
            "n_errors": st.n_plugin_errors + st.n_model_errors + (st.n_tool_errors if st.closed else 0),
            "token_in": None, "token_cached": None, "token_out": None, "ttft": None,
            "stats_id": None,
        }

    # ---------------------------------------------------------- 基础设施
    def _span_id(self, event: Any) -> str:
        trace = getattr(event, "trace", None)
        sid = getattr(trace, "span_id", None) if trace is not None else None
        return str(sid or f"eid-{id(event)}")

    def _sweep_temp(self) -> None:
        cutoff = self.clock() - 600
        for table in (self.received_at, self.prep_at):
            for k, v in list(table.items()):
                if v < cutoff:
                    table.pop(k, None)
        for k, v in list(self.first_req.items()):
            if v[0] < cutoff:
                self.first_req.pop(k, None)
        for k, v in list(self.handler_notes.items()):
            if v["t"] < cutoff:
                self.handler_notes.pop(k, None)
        for k, v in list(self.pre_calls.items()):
            if v["t"] < cutoff:
                self.pre_calls.pop(k, None)
        for k, v in list(self.closed_by_eid.items()):
            if v["t"] < cutoff:
                self.closed_by_eid.pop(k, None)

    def watchdog_interrupt_stale(self) -> list[str]:
        cutoff = self.clock() - max(1, self.cfg.stale_close_minutes) * 60
        out: list[str] = []
        with self._lock:
            for st in list(self.open_by_eid.values()):
                if not st.closed and st.last_event_at < cutoff:
                    st.closed = True
                    st.status = "interrupted"
                    st.end_at = self.clock()
                    self._align_ticks(st)
                    self._match_tool_times(st)
                    self._compute_step_times(st, st.end_at)
                    self._close(st)
                    out.append(st.run_id)
        return out

    def recover_interrupted(self) -> list[str]:
        rows = self.store.all_running()
        ids = [r["run_id"] for r in rows]
        if ids:
            self.store.mark_interrupted(ids)
            self.store.recompute_umos({r["umo"] for r in rows})
        return ids

    # ---------------------------------------------------------- stats 对齐 / 清理
    def align_provider_stats(self, astrbot_db: str | None) -> dict:
        """provider_stats 对齐：补 token / ttft / 失败状态。返回 {checked, linked, errors}。

        只读 astrbot 库（mode=ro）。条件：stats_id 为空、已关闭、begin_at 在 1 天内、
        source='live'；查同 umo、同 conversation_id、|start_time-begin_at|≤5 秒、
        未被别的运行占用的最近一行。
        """
        import sqlite3
        from pathlib import Path

        out = {"checked": 0, "linked": 0, "errors": 0}
        if not astrbot_db or not Path(astrbot_db).exists():
            return out
        try:
            src = sqlite3.connect(f"file:{Path(astrbot_db).as_posix()}?mode=ro", uri=True)
            src.row_factory = sqlite3.Row
        except Exception:
            self._log("warning", "stats 对齐：打不开 astrbot 库")
            return out
        try:
            tables = {r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "provider_stats" not in tables:
                return out
            now = self.clock()
            rows = self.store._read_conn().execute(
                "SELECT run_id, umo, cid, begin_at, status, error_text, n_errors FROM runs "
                "WHERE source='live' AND stats_id IS NULL AND status IN ('done','error') "
                "AND begin_at IS NOT NULL AND begin_at > ?",
                (now - 86400,)).fetchall()
            out["checked"] = len(rows)
            for r in rows:
                try:
                    used = self.store._read_conn().execute(
                        "SELECT stats_id FROM runs WHERE stats_id IS NOT NULL").fetchall()
                    used_ids = {u[0] for u in used}
                    cands = src.execute(
                        "SELECT id, status, provider_id, provider_model, "
                        "token_input_other, token_input_cached, token_output, "
                        "start_time, end_time, time_to_first_token "
                        "FROM provider_stats WHERE umo=? AND conversation_id=? "
                        "AND ABS(start_time-?)<=5 ORDER BY ABS(start_time-?) ASC",
                        (r["umo"], r["cid"], r["begin_at"], r["begin_at"])).fetchall()
                    hit = None
                    for c in cands:
                        if c["id"] not in used_ids:
                            hit = c
                            break
                    if hit is None:
                        continue
                    fields: dict = {
                        "token_in": hit["token_input_other"],
                        "token_cached": hit["token_input_cached"],
                        "token_out": hit["token_output"],
                        "ttft": hit["time_to_first_token"] or None,
                        "provider_id": hit["provider_id"],
                        "model": hit["provider_model"],
                        "stats_id": hit["id"],
                    }
                    if hit["status"] == "error" and r["status"] != "error":
                        fields["status"] = "error"
                        fields["n_errors"] = (r["n_errors"] or 0) + 1
                        if not r["error_text"]:
                            fields["error_text"] = "模型调用失败（provider_stats）"
                        # 补一条 error 事件
                        seq = self.store.max_error_event_seq(r["run_id"]) + 1
                        self.store.insert_events([{
                            "run_id": r["run_id"], "seq": seq,
                            "t": hit["end_time"] or None, "kind": "error",
                            "step": 0, "name": "provider_stats",
                            "text": "模型调用失败（provider_stats）", "is_error": 1,
                        }])
                        out["errors"] += 1
                    self.store.update_run(r["run_id"], fields)
                    self.store.recompute_umo(r["umo"])
                    out["linked"] += 1
                except Exception:
                    self._log("exception", f"stats 对齐失败 {r['run_id']}")
            return out
        finally:
            try:
                src.close()
            except Exception:
                pass

    def cleanup_retention(self, days: int) -> int:
        if days <= 0:
            return 0
        try:
            return self.store.purge(days)
        except Exception:
            self._log("exception", "retention 清理失败")
            return 0

    def _persist_note_error(self, event: Any | None, t: float, name: str, text: str) -> None:
        f = self._event_fields(event) if event is not None else {
            "umo": "", "platform_id": "", "message_type": "", "sender_id": "", "sender_name": ""}
        umo = f.get("umo") or ""
        if not umo:
            return
        run_id = f"note:{uuid.uuid4().hex[:12]}"
        display = self._name(umo)

        def job(store):
            store.touch_umo(umo, f.get("platform_id", ""), f.get("message_type", ""), display)
            store.insert_run({
                "run_id": run_id, "umo": umo, "cid": None, "source": "note",
                "status": "error", "sort_at": t, "ord": 0, "time_source": "hook",
                "sender_id": f.get("sender_id"), "sender_name": f.get("sender_name"),
                "begin_at": t, "end_at": t, "n_errors": 1, "error_text": text[:500],
            })
            store.insert_events([{
                "run_id": run_id, "seq": 1, "t": t, "kind": "error",
                "name": name, "text": text, "is_error": 1,
            }])
            store.recompute_umo(umo)
        self._enqueue(job)


def _near(v: float | None, target: float) -> bool:
    return v is not None and abs(v - target) < 1e-9
