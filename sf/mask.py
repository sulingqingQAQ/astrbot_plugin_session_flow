# -*- coding: utf-8 -*-
"""导出时屏蔽某一段（v0.1.8）。

设计取舍：
- 默认不屏蔽：没有规则就什么都不动，包括群聊最近消息那一块；
- 被屏蔽的段**整段删掉**，不留占位；文件头由导出模块写一句“有 N 段被屏蔽”；
- 规则存在插件自己的库里（`meta` 表，键 `export_mask_rules`），单轮导出和批量导出都生效；
- 屏蔽在后端做：被屏蔽的内容根本不进导出结果；
- 不做自动打码（号码、群名这些不动）。

规则 = {kind, name, on}：
- kind 是下面 KINDS 之一；
- name 写“名字 / 来源插件”，不区分大小写，可以带 * 通配；留空或 * = 这一种全部。
  注入匹配 注入块名（如 group_recent）和来源插件；工具匹配工具名；改动匹配插件名 / 改了什么；
  实际发出匹配改写它的插件；提问 / 回复 / 思考 / system prompt 没有名字，只能整种屏蔽（name 留空）。
- on 为 False 的规则存着但不生效。
另有“这一次”的临时屏蔽：导出时勾几个种类，整种屏蔽，不进库。
"""

from __future__ import annotations

import fnmatch
import json
from typing import Any

META_KEY = "export_mask_rules"
MAX_RULES = 100
MAX_NAME = 120

KINDS = ("system_prompt", "user", "text", "think", "tool", "inject", "edit", "sent")
KIND_TXT = {"system_prompt": "system prompt", "user": "提问", "text": "回复", "think": "思考",
            "tool": "工具（调用和结果）", "inject": "注入", "edit": "改动", "sent": "实际发出"}
NAMELESS = ("system_prompt", "user", "text", "think")   # 没有名字可匹配的种类

_EV_TO_RULE = {"user": "user", "text": "text", "think": "think", "tool_call": "tool", "tool_result": "tool",
               "inject": "inject", "edit": "edit", "sent": "sent"}


# =================================================================== 规则的存取
def clean_rules(raw: Any) -> list[dict]:
    """把页面提交的规则整理成规范形状；不认识的种类、重复的、太长的丢掉。"""
    out: list[dict] = []
    seen: set = set()
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("kind") or "").strip()
        if kind not in KINDS:
            continue
        name = str(r.get("name") or "").strip()[:MAX_NAME]
        if name == "*":
            name = ""
        if kind in NAMELESS:
            name = ""
        key = (kind, name.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "name": name, "on": bool(r.get("on", True))})
        if len(out) >= MAX_RULES:
            break
    return out


def load_rules(store: Any) -> list[dict]:
    try:
        return clean_rules(json.loads(store.get_meta(META_KEY) or "[]"))
    except (ValueError, TypeError):
        return []


def save_rules(store: Any, rules: Any) -> list[dict]:
    rules = clean_rules(rules)
    store.set_meta(META_KEY, json.dumps(rules, ensure_ascii=False))
    return rules


# =================================================================== 匹配
def rule_kind_of(ev: dict) -> str | None:
    return _EV_TO_RULE.get(str(ev.get("kind") or ""))


def names_of(ev: dict) -> list[str]:
    """一个事件上规则的 name 可以匹配的字符串（小写）。"""
    d = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    k = ev.get("kind")
    names: list[Any] = []
    if k == "inject":
        names = [ev.get("name"), d.get("plugin"), d.get("handler")]
    elif k in ("tool_call", "tool_result"):
        names = [ev.get("name")]
    elif k == "edit":
        names = [d.get("plugin"), ev.get("name"), d.get("handler")]
    elif k == "sent":
        for w in d.get("rewrites") or []:
            if isinstance(w, dict):
                names += [w.get("plugin"), w.get("handler")]
    return [str(n).lower() for n in names if n]


def _hit(rule: dict, ev: dict) -> bool:
    if rule_kind_of(ev) != rule["kind"]:
        return False
    pat = (rule.get("name") or "").strip().lower()
    if not pat:
        return True
    return any(fnmatch.fnmatchcase(n, pat) for n in names_of(ev))


def active_rules(rules: list[dict] | None, once_kinds: Any = None) -> list[dict]:
    """生效的规则：库里开着的 + 这一次临时勾选的整种屏蔽。"""
    out = [r for r in (rules or []) if r.get("on", True)]
    for k in once_kinds or []:
        if k in KINDS:
            out.append({"kind": k, "name": "", "on": True, "once": True})
    return out


# =================================================================== 应用
def apply(payload: dict, rules: list[dict]) -> tuple[dict, int]:
    """payload：webapi 的“单轮详情”（{run, events, sysprompt}，sysprompt 里已带 text）。
    → (屏蔽后的新 payload, 被屏蔽的段数)。没有规则时原样返回、段数 0。"""
    if not rules:
        return payload, 0
    run = dict(payload.get("run") or {})
    events = list(payload.get("events") or [])
    sp = payload.get("sysprompt")
    keep: list[dict] = []
    dropped: list[dict] = []
    for ev in events:
        (dropped if any(_hit(r, ev) for r in rules) else keep).append(ev)
    n = len(dropped)

    # 被屏蔽的注入如果也写在 system prompt / 提问里，那里一并抹掉，免得换个地方露出来
    sp_text = sp.get("text") if isinstance(sp, dict) else None
    user_text = run.get("user_text") or ""
    for ev in dropped:
        if ev.get("kind") != "inject" or not ev.get("text"):
            continue
        src = (ev.get("data") or {}).get("source") if isinstance(ev.get("data"), dict) else None
        if src == "system_prompt" and sp_text:
            sp_text = sp_text.replace(ev["text"], "", 1)
        elif src == "prompt" and user_text:
            user_text = user_text.replace(ev["text"], "", 1)
    kinds = {rule_kind_of(e) for e in dropped}

    # 整种屏蔽的提问 / 回复，运行记录里那份副本也要清掉
    if "user" in kinds:
        run["user_text"] = ""
    elif user_text != (run.get("user_text") or ""):
        run["user_text"] = user_text
    if "text" in kinds:
        run["reply_text"] = ""

    masked_sp = any(r["kind"] == "system_prompt" for r in rules)
    if masked_sp and sp:
        n += 1
        sp = None
    elif isinstance(sp, dict) and sp_text is not None and sp_text != sp.get("text"):
        sp = {**sp, "text": sp_text}
    out = {**payload, "run": run, "events": keep, "sysprompt": sp}
    out["masked_system_prompt"] = bool(masked_sp)
    return out, n


# =================================================================== 页面上“见过的名字”
def seen_names(store: Any, limit: int = 200) -> dict:
    """给规则编辑框做下拉提示：库里出现过的注入名 / 来源插件、工具名、改动插件、改写发出内容的插件。"""
    conn = store._read_conn()
    out: dict[str, list[dict]] = {"inject": [], "tool": [], "edit": [], "sent": []}

    def put(kind: str, name: Any, n: Any) -> None:
        if name:
            out[kind].append({"name": str(name), "n": int(n or 0)})

    try:
        for r in conn.execute("SELECT name, COUNT(*) n FROM events WHERE kind='inject' AND name IS NOT NULL "
                              "GROUP BY name ORDER BY n DESC LIMIT ?", (limit,)):
            put("inject", r[0], r[1])
        for r in conn.execute("SELECT json_extract(data,'$.plugin') p, COUNT(*) n FROM events WHERE kind='inject' "
                              "AND p IS NOT NULL GROUP BY p ORDER BY n DESC LIMIT ?", (limit,)):
            put("inject", r[0], r[1])
        for r in conn.execute("SELECT name, COUNT(*) n FROM events WHERE kind='tool_call' AND name IS NOT NULL "
                              "GROUP BY name ORDER BY n DESC LIMIT ?", (limit,)):
            put("tool", r[0], r[1])
        for r in conn.execute("SELECT json_extract(data,'$.plugin') p, COUNT(*) n FROM events WHERE kind='edit' "
                              "AND p IS NOT NULL GROUP BY p ORDER BY n DESC LIMIT ?", (limit,)):
            put("edit", r[0], r[1])
        for r in conn.execute("SELECT json_extract(w.value,'$.plugin') p, COUNT(*) n FROM events e, "
                              "json_each(e.data,'$.rewrites') w WHERE e.kind='sent' AND p IS NOT NULL "
                              "GROUP BY p ORDER BY n DESC LIMIT ?", (limit,)):
            put("sent", r[0], r[1])
    except Exception:
        pass   # 没有 JSON 函数就只给能取到的部分，不影响规则本身
    for k in out:
        seen: set = set()
        uniq = []
        for x in sorted(out[k], key=lambda x: -x["n"]):
            if x["name"].lower() not in seen:
                seen.add(x["name"].lower())
                uniq.append(x)
        out[k] = uniq[:limit]
    return out


def note_line(n: int) -> str:
    return f"有 {n} 段被屏蔽（按屏蔽规则整段删除，没留占位）"

