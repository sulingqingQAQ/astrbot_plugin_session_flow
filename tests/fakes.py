# -*- coding: utf-8 -*-
"""离线测试共用的假对象：形状照真实宿主（AstrBot 4.28 / 4.29）里抓到的数据。"""

from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _stubs
_stubs.install_astrbot_stub()

from sf import names as names_mod
from sf import recorder as recorder_mod
from sf import store as store_mod

UMO_WC = "webchat:FriendMessage:webchat!astrbot!test-uuid-0001"
UMO_QQ = "default:GroupMessage:100000999"


class FakeTrace:
    def __init__(self, span_id: str):
        self.span_id = span_id


class FakeMsgObj:
    def __init__(self, timestamp: float = 1700000000.0):
        self.timestamp = timestamp


class FakeEvent:
    def __init__(self, umo: str = UMO_WC, span_id: str = "span-1",
                 mtype: str = "FriendMessage", sender_id: str = "s1",
                 sender_name: str = "测试用户", ts: float = 1700000000.0,
                 platform_id: str | None = None):
        self.unified_msg_origin = umo
        self._mtype = mtype
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.message_obj = FakeMsgObj(ts)
        self.trace = FakeTrace(span_id)
        self.platform_id = platform_id or (umo.split(":", 1)[0] if umo else "")
        self._result = None

    def get_message_type(self):
        return self._mtype

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_result(self):
        return self._result

    def set_result(self, result):
        self._result = result
        return self


class FakeFuncTool:
    def __init__(self, names: list | None = None):
        self._names = names or []

    def names(self):
        return list(self._names)


class FakeConv:
    def __init__(self, cid: str = "cid-1", persona_id: str = "", title: str = ""):
        self.cid = cid
        self.persona_id = persona_id
        self.title = title


class FakeReq:
    def __init__(self, prompt: str = "你好", system_prompt: str = "你是助手",
                 cid: str = "cid-1", tools: list | None = None,
                 model: str = "test-model", contexts=None, image_urls=None):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.contexts = contexts or []
        self.func_tool = FakeFuncTool(tools)
        self.conversation = FakeConv(cid)
        self.image_urls = image_urls or []
        self.model = model


class FakeCtx:
    def __init__(self, messages: list):
        self.messages = messages


def user_msg(text: str, injects: list[str] | None = None) -> dict:
    parts = [{"type": "text", "text": text}]
    for inj in injects or []:
        parts.append({"type": "text", "text": inj})
    return {"role": "user", "content": parts}


def assistant_msg(text: str = "", think: str = "", tool_calls: list | None = None) -> dict:
    content = []
    if think:
        content.append({"type": "think", "think": think})
    if text:
        content.append({"type": "text", "text": text})
    m: dict = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


def tool_call_msg(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def tool_msg(content: str, call_id: str) -> dict:
    return {"role": "tool", "content": content, "tool_call_id": call_id}


class FakeResult:
    """decorating 的 MessageEventResult 桩。"""

    def __init__(self, text: str, ctype: str = "STREAMING_FINISH", is_llm: bool = False):
        self._text = text
        self.result_content_type = _CType(ctype)
        self._is_llm = is_llm

    def get_plain_text(self):
        return self._text

    def is_llm_result(self):
        return self._is_llm


class _CType:
    def __init__(self, name: str):
        self.name = name


class FakeResp:
    def __init__(self, text: str = "", usage: dict | None = None):
        self.completion_text = text
        self.reasoning_content = ""
        self.usage = usage or {}


def tmp_db_path(suffix: str = ".db") -> Path:
    """沙箱 python 在现建子目录里 sqlite 打不开：库文件直接落已存在的目录。"""
    base = Path(tempfile.tempdir) if tempfile.tempdir else Path(__file__).resolve().parent / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"sf-{uuid.uuid4().hex}{suffix}"


def make_recorder(cfg: dict | None = None, astrbot_db=None, log=None):
    dbp = tmp_db_path()
    st = store_mod.Store(dbp)
    nm = names_mod.NameResolver(astrbot_db)
    base = {"enable_record": True, "exclude_umos": [], "store_system_prompt": True,
            "max_text_chars": 50000, "max_tool_result_chars": 20000,
            "stale_close_minutes": 30}
    base.update(cfg or {})
    rc = recorder_mod.RecorderConfig.from_config(base, lambda c, k, d=None: c.get(k, d))
    rec = recorder_mod.Recorder(st, nm, rc, log=log)
    rec.astrbot_db = astrbot_db
    return rec, st, nm, str(dbp)


def drive_run(rec, umo: str = UMO_WC, span: str = "span-1", prompt: str = "掷个骰子",
              system_prompt: str = "你是助手", cid: str = "cid-1",
              new_messages: list | None = None, tool_uses: list | None = None,
              llm_resp_text: str = "好", clock=None):
    """走一遍完整钩子：waiting → req×2 → begin → (tool 起止)* → llm_response → done。返回 run_id。"""
    ev = FakeEvent(umo=umo, span_id=span)
    t = clock or rec.clock
    rec.on_waiting(ev)
    req = FakeReq(prompt=prompt, system_prompt=system_prompt, cid=cid)
    rec.on_request_first(ev)
    rec.on_request_last(ev, req)
    msgs: list = [{"role": "system", "content": system_prompt},
                  user_msg("历史问题"),
                  user_msg(prompt)]
    ctx = FakeCtx(msgs)
    rec.on_agent_begin(ev, ctx)
    for name, args, result in tool_uses or []:
        rec.on_tool_start(ev, _Tool(name), args)
        if result is not None:
            rec.on_tool_end(ev, _Tool(name), args, _ToolResult(result))
    rec.on_llm_response(ev, FakeResp(llm_resp_text))
    for m in new_messages or []:
        msgs.append(m)
    rec.on_agent_done(ev, ctx, FakeResp(llm_resp_text))
    st = rec.open_by_eid.get(id(ev))
    run_id = f"{span}#1"
    return run_id, ev


class _Tool:
    def __init__(self, name: str):
        self.name = name


class _ToolResult:
    def __init__(self, text: str, is_error: bool = False):
        self._text = text
        self.isError = is_error
        self.content = [{"type": "text", "text": text}]

    def model_dump(self):
        return {"content": [{"type": "text", "text": self._text}], "isError": self.isError}
