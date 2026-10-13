# -*- coding: utf-8 -*-
"""每轮时间拆分（v0.1.8）：把一轮从“用户发出消息”到“回复发出前”的时间切成几段。

数据来源（都是记录时就存下的时间点，这里只做减法）：
- 运行表里的 消息时间 msg_at / 收到时间 received_at / 请求钩子开始 prep_at / agent 开始 begin_at / agent 结束 end_at；
- `timing` 事件（phase=run）里的 最后一个请求钩子的时间 req_last_at、工具总耗时 tool_ms、别的插件处理函数的耗时；
- `timing` 事件（phase=after）里的 走到发送前最后一个钩子的时间 decorating_at，以及这段时间里别的插件处理函数的耗时。

分段（缺哪个时间点就少哪一段，不硬凑）：
    wait   聚合等待 / 平台延迟 = 收到时间 − 消息时间（含平台时间戳和本机时钟的差，所以只能当参考）
    prep   内核准备           = 请求钩子开始 − 收到时间（AstrBot 取会话、拼请求等）
    hooks  请求钩子           = 最后一个请求钩子 − 请求钩子开始（别的插件的 on_llm_request 都在这一段）
    start  起跑               = agent 开始 − 最后一个请求钩子
    model  模型               = (agent 结束 − agent 开始) − 工具总耗时
    tools  工具               = 工具总耗时
    after  回复后处理         = 发送前最后一个钩子 − agent 结束
发送后（on_after_message_sent）的耗时不在里面。
"""

from __future__ import annotations

import json
from typing import Any

HOOK_TXT = {
    "on_waiting_llm_request": "等待", "on_llm_request": "请求前", "on_agent_begin": "agent 开始",
    "on_using_llm_tool": "调工具前", "on_llm_tool_respond": "工具返回后", "on_llm_response": "模型回复后",
    "on_agent_done": "agent 结束", "on_decorating_result": "发送前", "on_after_message_sent": "发送后",
}

SEGMENTS = (
    ("wait", "聚合等待 / 平台延迟", "收到时间 − 消息时间。含聚合窗口、平台推送延迟和本机与平台的时钟差，只能当参考"),
    ("prep", "内核准备", "收到消息 → 开始跑请求钩子：AstrBot 取会话、拼请求、查知识库等"),
    ("hooks", "请求钩子", "各插件的 on_llm_request 处理函数，同步等在每次请求前"),
    ("start", "起跑", "请求钩子跑完 → agent 开始"),
    ("model", "模型", "agent 开始 → 结束，减去工具耗时"),
    ("tools", "工具", "各次工具调用的耗时之和"),
    ("after", "回复后处理", "agent 结束 → 走到发送前最后一个钩子（回复装饰、分段等）"),
)


def _data(ev: dict) -> dict:
    d = ev.get("data")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            d = {}
    return d if isinstance(d, dict) else {}


def _num(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def build(run: dict, events: list[dict]) -> dict | None:
    """→ {segments:[{key,label,hint,s}], total_s, calls:[…], notes:[…]}；这一轮没有计时事件就返回 None。"""
    tevs = [e for e in events if e.get("kind") == "timing"]
    if not tevs:
        return None
    runp = next((_data(e) for e in tevs if _data(e).get("phase") == "run"), {})
    after = next((_data(e) for e in tevs if _data(e).get("phase") == "after"), {})
    a = _num(run.get("msg_at"))
    b = _num(run.get("received_at")) or _num(runp.get("received_at"))
    c = _num(run.get("prep_at")) or _num(runp.get("prep_at"))
    d = _num(runp.get("req_last_at"))
    e_ = _num(run.get("begin_at")) or _num(runp.get("begin_at"))
    f = _num(run.get("end_at")) or _num(runp.get("end_at"))
    g = _num(after.get("decorating_at"))
    tool_s = (_num(runp.get("tool_ms")) or 0.0) / 1000.0

    def span(x: float | None, y: float | None) -> float | None:
        return None if x is None or y is None else y - x

    run_s = span(e_, f)
    raw = {
        "wait": span(a, b), "prep": span(b, c), "hooks": span(c, d), "start": span(d, e_),
        "model": None if run_s is None else run_s - tool_s,
        "tools": tool_s if run_s is not None else None,
        "after": span(f, g),
    }
    segs = []
    notes = []
    for key, label, hint in SEGMENTS:
        v = raw.get(key)
        if v is None:
            continue
        if v < 0:
            if key == "wait":
                notes.append("消息时间比收到时间还晚，是平台和本机时钟有差，“聚合等待”按 0 算")
            v = 0.0
        segs.append({"key": key, "label": label, "hint": hint, "s": round(v, 3)})
    merged: dict[tuple, dict] = {}
    for src in (runp.get("calls") or [], after.get("calls") or []):
        for c_ in src:
            k = (c_.get("hook"), c_.get("plugin"), c_.get("handler"))
            m = merged.get(k)
            if m is None:
                merged[k] = dict(c_)
            else:
                m["n"] = m.get("n", 0) + c_.get("n", 0)
                m["total_ms"] = round(m.get("total_ms", 0) + c_.get("total_ms", 0), 1)
                m["max_ms"] = max(m.get("max_ms", 0), c_.get("max_ms", 0))
    calls = sorted(merged.values(), key=lambda x: -x.get("total_ms", 0))
    for c_ in calls:
        c_["hook_label"] = HOOK_TXT.get(c_.get("hook"), c_.get("hook"))
    if not after:
        notes.append("没有“回复后处理”：这一轮没走到发送前（模型出错、被打断或旧版本记录）")
    notes.append("发送后的处理函数耗时不计入；只统计会话流包装得到的别的插件处理函数")
    return {"segments": segs, "total_s": round(sum(s["s"] for s in segs), 3), "calls": calls, "notes": notes,
            "hook_calls_total_ms": round(sum(c_.get("total_ms", 0) for c_ in calls), 1)}
