# -*- coding: utf-8 -*-
"""消息解析：把 AstrBot / OpenAI 形状的消息内容拆成会话流事件要素。

输入可以是 pydantic 对象（实时钩子）也可以是 dict（旧档 conversations.content）。
所有转换纯函数，不碰数据库、不做 IO。
"""

from __future__ import annotations

import json
import re
from typing import Any

TAG_RE = re.compile(r"<([A-Za-z][\w-]*)>(.*?)</\1>", re.DOTALL)
BASE64_RE = re.compile(r"(data:image/[a-zA-Z0-9.+-]+;base64,)([A-Za-z0-9+/=\s]{8,})")


def model_obj(value: Any) -> Any:
    """pydantic / dataclass → dict；dict 原样；其余 None。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            return None
    try:
        import dataclasses

        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return dataclasses.asdict(value)
    except Exception:
        pass
    return None


def strip_images_in_text(text: str) -> tuple[str, int]:
    """正文中的 data:image base64 换成占位符。返回 (新文本, 替换处数)。"""
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        count += 1
        raw = re.sub(r"\s", "", m.group(2))
        kb = max(1, round(len(raw) * 3 / 4 / 1024))
        return f"[base64 图片已省略，约 {kb} KB]"

    return BASE64_RE.sub(repl, text), count


def truncate(text: str, limit: int) -> tuple[str, dict | None]:
    """超过 limit 截断并标注原长。system prompt 不走这里。"""
    if limit <= 0 or len(text) <= limit:
        return text, None
    return text[:limit] + f"…（已截断，原长 {len(text)} 字）", {"truncated": len(text)}


def part_kind(part: Any) -> str:
    d = model_obj(part)
    if isinstance(d, dict):
        return str(d.get("type") or "")
    return ""


def part_text(part: Any) -> str:
    d = model_obj(part)
    if not isinstance(d, dict):
        return str(part or "")
    if d.get("type") == "text":
        return str(d.get("text") or "")
    if d.get("type") == "think":
        return str(d.get("think") or "")
    return ""


def inject_name(text: str) -> str:
    """注入块名：取开头 <标签>，没有就叫 inject。"""
    m = re.match(r"\s*<([A-Za-z][\w-]*)>", text or "")
    return m.group(1) if m else "inject"


def _split_blocks(text: str, fallback_name: str, source: str) -> list[dict]:
    """把一段追加进来的文本拆成注入块：<Tag>…</Tag> 按标签命名，其余按 fallback_name。"""
    out: list[dict] = []
    last = 0
    for m in TAG_RE.finditer(text):
        rest = text[last:m.start()]
        if rest.strip():
            out.append({"name": fallback_name, "text": rest.strip("\n"), "source": source})
        out.append({"name": m.group(1), "text": m.group(0), "source": source})
        last = m.end()
    rest = text[last:]
    if rest.strip():
        out.append({"name": fallback_name, "text": rest.strip("\n"), "source": source})
    return out


def diff_injections(base: str | None, final: str | None, label: str, source: str) -> list[dict]:
    """某段文本（system prompt / 提问）在别的插件动过手之后，多出来的部分当注入返回。

    base 是最先拿到的原文，final 是最终发给模型的。label 是展示用的名字（如“系统提示词”）。
    - final 里包含 base：base 前面多的算“前置”，后面多的算“追加”；
    - 否则取最长公共前后缀，中间新增的部分算“改写”（被换掉的原文放 removed）。
    base 为 None（没拿到快照）或没变化 → 空。
    """
    if base is None or final is None or base == final:
        return []
    out: list[dict] = []
    if not base:
        return _split_blocks(final, label + "追加", source)
    i = final.find(base)
    if i >= 0:
        pre, suf = final[:i], final[i + len(base):]
        out += _split_blocks(pre, label + "前置", source)
        out += _split_blocks(suf, label + "追加", source)
        return out
    p = 0
    n = min(len(base), len(final))
    while p < n and base[p] == final[p]:
        p += 1
    q = 0
    while q < n - p and base[len(base) - 1 - q] == final[len(final) - 1 - q]:
        q += 1
    added = final[p:len(final) - q]
    removed = base[p:len(base) - q]
    if added.strip():
        blocks = _split_blocks(added, label + "改写", source)
        for b in blocks:
            if removed.strip():
                b["removed"] = removed[:2000]
        out += blocks
    return out


def subtract_texts(text: str, parts: list[str]) -> str:
    """text 里去掉已经有人认领的片段（每个只去一次），剩下的才是没人认领的部分。

    两个处理函数连着往 system prompt 里追加没有标签的文字时，首尾快照的差是合成的一整块，
    和任何一个处理函数记的那一段都不相等，不减掉就会多出一项“未归属”的重复。
    """
    rest = text or ""
    for p in parts:
        if p and p in rest:
            rest = rest.replace(p, "", 1)
    return rest


def split_user_content(content: Any) -> tuple[str, list[dict], int]:
    """拆本轮 user 消息内容 → (原话, 注入块列表[{name,text}], 图片数)。

    - 列表：第一个 text 块是原话，其余 text 块算注入；image_url 块计数。
    - 字符串：按 <Tag>...</Tag> 拆，块前文字是原话，标签块算注入。
    """
    images = 0
    if isinstance(content, list):
        spoken, injects = "", []
        for part in content:
            kind = part_kind(part)
            if kind == "text":
                text = part_text(part)
                if not spoken:
                    spoken = text
                elif text.strip():
                    injects.append({"name": inject_name(text), "text": text})
            elif kind == "image_url":
                images += 1
            elif kind == "think":
                # 正常不会出现在 user 里，出现了也别丢
                text = part_text(part)
                if text.strip():
                    injects.append({"name": "think", "text": text})
            elif kind == "audio_url":
                images += 1
        return spoken, injects, images
    text = content if isinstance(content, str) else ""
    spoken_parts, injects = [], []
    last = 0
    for m in TAG_RE.finditer(text):
        if m.start() > last:
            spoken_parts.append(text[last:m.start()])
        body = m.group(0)
        injects.append({"name": m.group(1), "text": body})
        last = m.end()
    if last < len(text):
        spoken_parts.append(text[last:])
    spoken = "".join(spoken_parts)
    if not injects:
        spoken = text
    return spoken, injects, images


def assistant_segments(content: Any) -> list[dict]:
    """assistant 消息内容 → [{kind:'think'|'text', 'text':...}]，空内容返回 []。"""
    out: list[dict] = []
    if isinstance(content, str):
        if content:
            out.append({"kind": "text", "text": content})
        return out
    if isinstance(content, list):
        for part in content:
            kind = part_kind(part)
            text = part_text(part)
            if kind in ("think", "text") and text:
                out.append({"kind": kind, "text": text})
            elif kind not in ("think", "text"):
                d = model_obj(part)
                if d is not None and kind:
                    out.append({"kind": "text", "text": f"[{kind} 内容]"})
    return out


def tool_calls_of(message: dict) -> list[dict]:
    """从 assistant 消息 dict 取 [{id,name,arguments}]。"""
    out = []
    for tc in message.get("tool_calls") or []:
        d = model_obj(tc)
        if not isinstance(d, dict):
            continue
        fn = d.get("function") or {}
        if not isinstance(fn, dict):
            fn = {}
        out.append({
            "id": d.get("id") or "",
            "name": str(fn.get("name") or ""),
            "arguments": fn.get("arguments"),
        })
    return out


def tool_result_text(result: Any, limit: int = 20000) -> tuple[str, bool, dict | None]:
    """on_llm_tool_respond 的 tool_result → (文本, is_error, 截断data)。"""
    d = model_obj(result)
    is_error = bool(getattr(result, "isError", False))
    text = ""
    if isinstance(result, str):
        text = result
    elif isinstance(d, dict):
        is_error = bool(d.get("isError", is_error))
        content = d.get("content")
        if isinstance(content, list):
            parts = []
            for p in content:
                pd = model_obj(p)
                if isinstance(pd, dict) and pd.get("type") == "text":
                    parts.append(str(pd.get("text") or ""))
                elif isinstance(p, str):
                    parts.append(p)
            text = "\n".join(parts)
        elif content is not None:
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        elif "text" in d:
            text = str(d["text"])
    else:
        text = str(result)
    text, cut = truncate(text, limit)
    return text, is_error, cut


def error_tool_result(content: Any, limit: int = 20000) -> tuple[str, dict | None]:
    """tool 消息内容（报错时形如 'error: xxx'）→ 文本 + 截断信息。"""
    text = content if isinstance(content, str) else json.dumps(model_obj(content), ensure_ascii=False, default=str)
    return truncate(text, limit)


def pretty_json_args(arguments: Any) -> str:
    """工具参数格式化成给页面看的字符串。"""
    if arguments is None or arguments == "":
        return ""
    if isinstance(arguments, str):
        try:
            obj = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return arguments
    else:
        obj = arguments
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(obj)


def is_error_result_text(text: str) -> bool:
    return (text or "").lstrip().startswith("error:")


# =================================================================== v0.1.5：回复 / 发出内容 / 请求其他字段的快照与比较

_MARK = {
    "Image": "图片", "Record": "语音", "Video": "视频", "File": "文件", "Json": "卡片",
    "Forward": "转发", "Node": "转发节点", "Nodes": "转发", "Poke": "戳一戳", "Share": "分享",
    "Music": "音乐", "Face": "表情", "Reply": "引用", "AtAll": "@全体",
}
# 比较“发出去的内容变没变”时不算的成分：@ / 引用 / 表情常由宿主或平台自己加
_SOFT_COMPS = {"Plain", "At", "AtAll", "Reply", "Face"}


def chain_sig(chain: Any) -> tuple:
    """消息链 → 可比较的签名 ((类名, 文本)…)：Plain 留文本，At 留对象，其余只留类名。"""
    out = []
    for comp in list(chain or []):
        name = type(comp).__name__
        if name == "Plain":
            out.append(("Plain", str(getattr(comp, "text", "") or "")))
        elif name == "At":
            out.append(("At", str(getattr(comp, "qq", "") or getattr(comp, "name", "") or "")))
        else:
            out.append((name, ""))
    return tuple(out)


def result_sig(result: Any) -> tuple:
    """MessageEventResult / MessageChain / None → 签名。取不到 chain 就退回纯文本（测试桩或旧版）。"""
    if result is None:
        return ()
    chain = getattr(result, "chain", None)
    if chain is not None:
        return chain_sig(chain)
    try:
        text = result.get_plain_text() or ""
    except Exception:
        text = ""
    return (("Plain", str(text)),) if text else ()


def chain_text(sig: tuple) -> str:
    """签名 → 给人看的文字：Plain 原样拼，其余成分写成 [图片] 这样的标记。"""
    parts = []
    for name, text in sig:
        if name == "Plain":
            parts.append(text)
        elif name == "At":
            parts.append(f"[@{text}]" if text else "[@]")
        else:
            parts.append(f"[{_MARK.get(name, name)}]")
    return "".join(parts)


def norm_text(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "")


def sent_signature(sig: tuple) -> tuple[str, tuple]:
    """签名 → (去空白的纯文本, 排好序的“实质性”非文本成分类名)。@ / 引用 / 表情不算。"""
    plain = norm_text("".join(t for n, t in sig if n == "Plain"))
    media = tuple(sorted(n for n, _ in sig if n not in _SOFT_COMPS))
    return plain, media


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for p in content:
            d = model_obj(p)
            if isinstance(d, dict):
                parts.append(str(d.get("text") or d.get("think") or ""))
            elif isinstance(d, str):
                parts.append(d)
        return "".join(parts)
    return str(content)


def ctx_fingerprint(contexts: Any, snip: int = 120) -> list[tuple]:
    """历史消息列表 → [(角色, 前 snip 字, 全文长度)…]。便宜，不拷贝正文。"""
    out = []
    for m in list(contexts or []):
        d = model_obj(m)
        if not isinstance(d, dict):
            out.append(("?", str(m)[:snip], len(str(m))))
            continue
        text = _content_text(d.get("content"))
        out.append((str(d.get("role") or "?"), text[:snip], len(text)))
    return out


def diff_contexts(before: list[tuple], after: list[tuple]) -> dict | None:
    """两份历史指纹 → {before, after, removed:[…], added:[…], reordered} 或 None（没变）。

    按多重集比较，保留原来的先后顺序；某个插件把每条都换成拷贝也不会被当成“全删全加”。
    """
    if before == after:
        return None
    left = {}
    for x in after:
        left[x] = left.get(x, 0) + 1
    removed = []
    for x in before:
        if left.get(x, 0) > 0:
            left[x] -= 1
        else:
            removed.append(x)
    left = {}
    for x in before:
        left[x] = left.get(x, 0) + 1
    added = []
    for x in after:
        if left.get(x, 0) > 0:
            left[x] -= 1
        else:
            added.append(x)
    return {"before": len(before), "after": len(after), "removed": removed, "added": added,
            "reordered": not removed and not added}


def describe_contexts(d: dict, limit: int = 5) -> str:
    def one(x):
        s = x[1].replace("\n", " ")
        return f"{x[0]}「{s}{'…' if x[2] > len(x[1]) else ''}」"

    lines = [f"历史 {d['before']} → {d['after']} 条"]
    if d["reordered"]:
        lines.append("条数和内容没变，只是顺序变了")
    if d["removed"]:
        lines.append(f"删去 {len(d['removed'])} 条：" + " · ".join(one(x) for x in d["removed"][:limit])
                     + ("…" if len(d["removed"]) > limit else ""))
    if d["added"]:
        lines.append(f"新增或改写 {len(d['added'])} 条：" + " · ".join(one(x) for x in d["added"][:limit])
                     + ("…" if len(d["added"]) > limit else ""))
    return "\n".join(lines)


def diff_names(before: list[str], after: list[str]) -> dict | None:
    """工具名列表前后比较 → {before, after, removed, added} 或 None。"""
    b, a = list(before or []), list(after or [])
    if sorted(b) == sorted(a):
        return None
    return {"before": len(b), "after": len(a),
            "removed": [x for x in b if x not in a], "added": [x for x in a if x not in b]}


def describe_names(d: dict, label: str) -> str:
    lines = [f"{label} {d['before']} → {d['after']} 个"]
    if d["removed"]:
        lines.append("去掉：" + "、".join(d["removed"][:30]))
    if d["added"]:
        lines.append("新增：" + "、".join(d["added"][:30]))
    return "\n".join(lines)
