# -*- coding: utf-8 -*-
"""上下文体积构成（v0.1.8）：一次请求发给模型的东西里，各部分各有多大。

量的和估的分开：
- measure()：在记录时对 req.contexts 等量一遍，只数**条数和字数**（中文字符和其余字符分开数，因为一个中文字
  对应的 token 比一个英文字母多得多）、工具调用条数、图片张数、最长几条。存成 `ctx` 事件，不存历史正文。
- estimate()：在页面 / 导出时现算，不入库。给中文字符和其余字符各一个权重，算出各部分的“加权字数”，
  再用服务商报的总输入 token 求一个换算系数，把各部分折算成 token。总数是服务商报的（准），
  各部分的占比是折算出来的（估算），没有服务商总数时只给字数和条数。

不含图片的 token（图片只数张数）；最后一个钩子之后宿主若再裁剪历史，量到的会偏多。
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import extract as ex

W_CJK = 1.0      # 一个中文字符折多少“单位”
W_OTHER = 0.3    # 一个非中文字符折多少“单位”（英文约 3~4 个字母一个 token）
HEAD_CHARS = 60
CHECKPOINT_ROLE = "_checkpoint"
TOP_N = 5
VERSION = 1


def count(text: Any) -> tuple[int, int]:
    """→ (中文等宽字符数, 其余字符数)。

    “中文等宽字符”指 UTF-8 里占 3 字节及以上的字符：汉字、假名、谚文、全角标点、emoji 等，一个字大约一个 token 或更多；
    其余是英文字母、数字、半角标点和拉丁 / 西里尔字母，几个字符才一个 token。
    用三次整串编码的长度解方程算出来（不逐字符循环）：百万字约几毫秒。
    """
    if not text:
        return 0, 0
    if not isinstance(text, str):
        text = str(text)
    n = len(text)
    if text.isascii():
        return 0, n
    try:
        b = len(text.encode("utf-8"))
        a = len(text.encode("ascii", "ignore"))
        u = len(text.encode("utf-16-le")) // 2
    except UnicodeError:   # 带孤立代理字符的怪文本：退回逐字符数
        wide = sum(1 for ch in text if ch >= "ࠀ")
        return wide, n - wide
    c4 = u - n                        # 4 字节（UTF-16 里是代理对）的字符数
    c3 = b - 2 * n + a - 2 * c4       # 3 字节的字符数
    wide = c3 + c4
    return wide, n - wide


def _add(acc: dict, text: Any) -> int:
    c, o = count(text)
    acc["cjk"] += c
    acc["other"] += o
    return c + o


def _bucket() -> dict:
    return {"n": 0, "cjk": 0, "other": 0}


def _msg_text_and_images(content: Any, acc: dict) -> tuple[int, int, str]:
    """一条消息的 content → (字数, 图片数, 开头预览)。content 可以是字符串，也可以是片段列表。"""
    if content is None:
        return 0, 0, ""
    if isinstance(content, str):
        return _add(acc, content), 0, content[:HEAD_CHARS]
    chars = images = 0
    head = ""
    if isinstance(content, (list, tuple)):
        for p in content:
            if isinstance(p, str):
                chars += _add(acc, p)
                head = head or p[:HEAD_CHARS]
                continue
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t in ("image_url", "image", "input_image"):
                images += 1
            elif t in ("audio_url", "input_audio", "audio"):
                continue
            else:
                s = p.get("text")
                if s is None:
                    s = p.get("think")
                if s:
                    chars += _add(acc, s)
                    head = head or str(s)[:HEAD_CHARS]
        return chars, images, head
    return _add(acc, str(content)), 0, str(content)[:HEAD_CHARS]


def tools_json(func_tool: Any) -> str | None:
    """工具定义序列化后的文本；接口不存在或出错返回 None（页面显示“没数到”）。"""
    fn = getattr(func_tool, "openai_schema", None)
    if not callable(fn):
        return None
    try:
        return json.dumps(fn(), ensure_ascii=False, default=str)
    except Exception:
        return None


def measure(contexts: list | None, *, system_prompt: str = "", prompt: str = "",
            extras: list[str] | None = None, tools_text: str | None = None,
            n_tools: int | None = None, image_urls: int = 0) -> dict:
    """对一次请求的各部分数条数和字数。纯函数，传入的列表自己先拷一份再给。"""
    t0 = time.perf_counter()
    roles: dict[str, dict] = {}
    images = 0
    tool_calls = 0
    checkpoints = 0
    top: list[tuple[int, int, str, str]] = []   # (字数, 序号, 角色, 开头)
    msgs = list(contexts or [])
    for i, m in enumerate(msgs):
        if m is not None and not isinstance(m, (dict, str)):
            m = ex.model_obj(m) or m          # pydantic / dataclass 形状的消息先转成字典
        if isinstance(m, dict) and m.get("role") == CHECKPOINT_ROLE:
            checkpoints += 1      # AstrBot 内部的存档标记：会并到前一条消息上，不当成一条消息发给模型，不算大小
            continue
        if not isinstance(m, dict):
            role, content, calls = "other", str(m), None
            d = {}
        else:
            d = m
            role = str(d.get("role") or "other")
            content = d.get("content")
            calls = d.get("tool_calls")
        b = roles.setdefault(role, _bucket())
        b["n"] += 1
        chars, imgs, head = _msg_text_and_images(content, b)
        images += imgs
        rc = d.get("reasoning_content")
        if rc:
            chars += _add(b, rc)
        if isinstance(calls, (list, tuple)) and calls:
            tool_calls += len(calls)
            b["tool_calls"] = b.get("tool_calls", 0) + len(calls)
            for c in calls:
                f = c.get("function") if isinstance(c, dict) else None
                if isinstance(f, dict):
                    chars += _add(b, f.get("name"))
                    chars += _add(b, f.get("arguments"))
                    head = head or ("调 " + str(f.get("name") or ""))
        if chars and (len(top) < TOP_N or chars > top[-1][0]):
            top.append((chars, i, role, head.replace("\n", " ")))
            top.sort(key=lambda x: -x[0])
            del top[TOP_N:]

    sysb, promptb, extrab, toolb = _bucket(), _bucket(), _bucket(), _bucket()
    _add(sysb, system_prompt)
    sysb["n"] = 1 if system_prompt else 0
    _add(promptb, prompt)
    promptb["n"] = 1 if prompt else 0
    for x in extras or []:
        _add(extrab, x)
        extrab["n"] += 1
    if tools_text is not None:
        _add(toolb, tools_text)
        toolb["n"] = int(n_tools or 0)
    return {
        "v": VERSION,
        "n_msgs": len(msgs) - checkpoints,
        "checkpoints": checkpoints,
        "roles": roles,
        "images": images,
        "tool_calls": tool_calls,
        "top": [{"i": i, "role": r, "chars": c, "head": h} for c, i, r, h in top],
        "system_prompt": sysb,
        "prompt": promptb,
        "extras": extrab,
        "tools": toolb if tools_text is not None else None,
        "request_images": int(image_urls or 0),
        "ms": round((time.perf_counter() - t0) * 1000.0, 2),
    }


# =================================================================== 折算（页面 / 导出时现算）

ROLE_TXT = {"user": "历史·用户", "assistant": "历史·助手", "tool": "历史·工具结果",
            "system": "历史·系统", "other": "历史·其他"}


def _w(b: dict | None) -> float:
    return (b or {}).get("cjk", 0) * W_CJK + (b or {}).get("other", 0) * W_OTHER


def estimate(ctx: dict | None, provider_total: int | None) -> dict | None:
    """ctx 事件的 data + 服务商报的总输入 token（未命中缓存 + 命中缓存）→ 各部分的表和说明。

    每一行：{key, label, n, cjk, other, chars, weight, share, tokens}。
    share / tokens 只有在有服务商总数时才有，是折算出来的估算值，各行 tokens 之和等于服务商总数。
    """
    if not isinstance(ctx, dict):
        return None
    rows: list[dict] = []

    def add(key: str, label: str, b: dict | None) -> None:
        if not b:
            return
        if not (b.get("n") or b.get("cjk") or b.get("other")):
            return
        rows.append({"key": key, "label": label, "n": b.get("n", 0), "cjk": b.get("cjk", 0),
                     "other": b.get("other", 0), "chars": b.get("cjk", 0) + b.get("other", 0),
                     "weight": _w(b), "tool_calls": b.get("tool_calls", 0)})

    add("system_prompt", "system prompt", ctx.get("system_prompt"))
    tools = ctx.get("tools")
    add("tools", "工具定义", tools)
    for role in ("system", "user", "assistant", "tool", "other"):
        b = (ctx.get("roles") or {}).get(role)
        if b:
            add("role:" + role, ROLE_TXT.get(role, role), b)
    for role, b in (ctx.get("roles") or {}).items():
        if role not in ROLE_TXT:
            add("role:" + role, "历史·" + role, b)
    add("prompt", "本轮提问", ctx.get("prompt"))
    add("extras", "本轮附加块（注入）", ctx.get("extras"))

    total_w = sum(r["weight"] for r in rows)
    out: dict[str, Any] = {
        "rows": rows, "total_chars": sum(r["chars"] for r in rows), "total_weight": total_w,
        "n_msgs": ctx.get("n_msgs"), "images": ctx.get("images", 0), "tool_calls": ctx.get("tool_calls", 0),
        "top": ctx.get("top") or [], "tools_counted": tools is not None, "provider_total": None,
        "scale": None, "note": "", "ms": ctx.get("ms"),
        "weights": {"cjk": W_CJK, "other": W_OTHER},
    }
    notes = []
    out["checkpoints"] = ctx.get("checkpoints", 0)
    if out["checkpoints"]:
        notes.append(f"历史列表里另有 {out['checkpoints']} 条 AstrBot 内部的 checkpoint 标记，不发给模型，没算进条数和字数")
    if tools is None:
        notes.append("工具定义没数到（这个 AstrBot 版本拿不到工具定义，或本轮没有工具），下面的占比不含它")
    if ctx.get("images") or ctx.get("request_images"):
        notes.append("图片只数了张数，图片占的 token 没算进各行")
    if provider_total and provider_total > 0 and total_w > 0:
        k = provider_total / total_w
        for r in rows:
            r["share"] = r["weight"] / total_w
            r["tokens"] = int(round(r["weight"] * k))
        out["provider_total"] = int(provider_total)
        out["scale"] = round(k, 3)
        if k < 0.5 or k > 2.0:
            notes.append(f"估算和服务商报的总量差得比较多（每个加权字折 {k:.2f} 个 token），"
                         "可能有图片、没数到的内容，或宿主在最后一个钩子之后又改了请求；占比仅供参考")
    else:
        notes.append("这一轮还没对上服务商的 token 统计，只给字数和条数")
    notes.append("占比是折算的估算：中文字符按 1、其余字符按 0.3 加权，再按服务商报的总输入 token 等比例折算；总数准，各行不是精确值")
    out["note"] = "；".join(notes)
    return out
