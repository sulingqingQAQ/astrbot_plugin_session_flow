# -*- coding: utf-8 -*-
"""按插件统计注入量（v0.1.8）：谁在往上下文里塞东西、塞了多少。

数据就是已经记下来的 `inject` 事件（来源见 recorder / attrib）：
- 精确归属的按“插件”算；
- “内核 / 更早”（会话流拍第一张快照时就已经在的）、“未归属”（没抓到是谁）各算一行；
- 没精确归属、但在已装插件源码里找到同名标签的，算“疑似 某插件”（只是推测）。
只按字数算，不换算 token（想看某一轮的 token 构成去那一轮的详情）。
另附“改动”统计：哪个插件改了历史 / 工具集 / 图片 / 模型，历史一共被删了多少条。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

_HIST = re.compile(r"历史\s*(\d+)\s*→\s*(\d+)\s*条")

LABEL_EARLIER = "内核 / 更早"
LABEL_UNATTR = "未归属"


def _label(attrib: Any, plugin: Any, name: Any, guesser: Any) -> tuple[str, str]:
    """→ (种类, 显示名)。种类：plugin / earlier / guess / unattributed。"""
    if attrib == "exact" and plugin:
        return "plugin", str(plugin)
    if attrib == "earlier":
        return "earlier", LABEL_EARLIER
    if guesser is not None and name:
        try:
            cand = guesser.guess(name)
        except Exception:
            cand = []
        if cand:
            return "guess", "疑似 " + str(cand[0])
    return "unattributed", LABEL_UNATTR


def _rows(conn, sql_json: str, sql_plain: str, args: list) -> list[tuple]:
    """优先用 SQLite 的 json_extract 直接取字段；没有 JSON 函数就取整列自己解析。"""
    try:
        return [tuple(r) for r in conn.execute(sql_json, args).fetchall()]
    except Exception:
        out = []
        for r in conn.execute(sql_plain, args).fetchall():
            try:
                d = json.loads(r[-1]) if r[-1] else {}
            except ValueError:
                d = {}
            d = d if isinstance(d, dict) else {}
            out.append((*r[:-1], d.get("attrib"), d.get("plugin"), d.get("source"), d.get("what")))
        return out


def inject_stats(store: Any, guesser: Any = None, *, days: int | None = None, umo: str | None = None,
                 now: float | None = None) -> dict:
    """days：只统计最近 N 天的回合，None = 全部；umo：只统计一个会话。"""
    conn = store._read_conn()
    since = (now if now is not None else time.time()) - days * 86400 if days else None
    where = " AND COALESCE(r.sort_at,0) >= ?" if since is not None else ""
    args: list = [since] if since is not None else []
    if umo:
        where += " AND r.umo = ?"
        args.append(umo)

    live = conn.execute(
        "SELECT COUNT(*) FROM runs r WHERE r.source='live'" + where, args).fetchone()[0]

    base_json = ("SELECT e.run_id, e.name, LENGTH(e.text), json_extract(e.data,'$.attrib'), "
                 "json_extract(e.data,'$.plugin'), json_extract(e.data,'$.source'), json_extract(e.data,'$.what') "
                 "FROM events e JOIN runs r ON r.run_id=e.run_id WHERE e.kind=?" + where)
    base_plain = ("SELECT e.run_id, e.name, LENGTH(e.text), e.data "
                  "FROM events e JOIN runs r ON r.run_id=e.run_id WHERE e.kind=?" + where)

    groups: dict[str, dict] = {}
    total_n = total_chars = 0
    runs_any: set = set()
    for run_id, name, length, attrib, plugin, source, _what in _rows(
            conn, base_json, base_plain, ["inject"] + args):
        kind, label = _label(attrib, plugin, name, guesser)
        g = groups.setdefault(label, {"label": label, "kind": kind, "n": 0, "chars": 0, "runs": set(),
                                      "by_source": {}, "names": {}})
        n_chars = int(length or 0)
        g["n"] += 1
        g["chars"] += n_chars
        g["runs"].add(run_id)
        src = source or "?"
        g["by_source"][src] = g["by_source"].get(src, 0) + n_chars
        nm = str(name or "inject")
        g["names"][nm] = g["names"].get(nm, 0) + 1
        total_n += 1
        total_chars += n_chars
        runs_any.add(run_id)

    rows = []
    for g in groups.values():
        runs_n = len(g["runs"])
        rows.append({
            "label": g["label"], "kind": g["kind"], "n": g["n"], "chars": g["chars"], "runs": runs_n,
            "avg_per_run": round(g["chars"] / runs_n) if runs_n else 0,
            "share": round(g["chars"] / total_chars, 4) if total_chars else 0.0,
            "presence": round(runs_n / live, 4) if live else 0.0,
            "by_source": g["by_source"],
            "names": sorted(g["names"].items(), key=lambda kv: -kv[1])[:8],
        })
    rows.sort(key=lambda r: -r["chars"])

    edits: dict[tuple, dict] = {}
    edit_json = ("SELECT e.run_id, json_extract(e.data,'$.plugin'), json_extract(e.data,'$.what'), e.text "
                 "FROM events e JOIN runs r ON r.run_id=e.run_id WHERE e.kind=?" + where)
    edit_plain = ("SELECT e.run_id, e.text, e.data FROM events e JOIN runs r ON r.run_id=e.run_id "
                  "WHERE e.kind=?" + where)
    try:
        edit_rows = [tuple(r) for r in conn.execute(edit_json, ["edit"] + args).fetchall()]
    except Exception:
        edit_rows = []
        for run_id, text, data in conn.execute(edit_plain, ["edit"] + args).fetchall():
            try:
                d = json.loads(data) if data else {}
            except ValueError:
                d = {}
            edit_rows.append((run_id, (d or {}).get("plugin"), (d or {}).get("what"), text))
    for run_id, plugin, what, text in edit_rows:
        key = (str(plugin or "?"), str(what or "?"))
        g = edits.setdefault(key, {"plugin": key[0], "what": key[1], "n": 0, "runs": set(), "removed": 0})
        g["n"] += 1
        g["runs"].add(run_id)
        if key[1] == "历史":
            m = _HIST.search(text or "")
            if m:
                g["removed"] += max(0, int(m.group(1)) - int(m.group(2)))
    edit_out = sorted(({"plugin": g["plugin"], "what": g["what"], "n": g["n"], "runs": len(g["runs"]),
                        "removed": g["removed"]} for g in edits.values()), key=lambda x: -x["n"])

    return {"days": days, "umo": umo, "live_runs": live, "runs_with_inject": len(runs_any),
            "inject_n": total_n, "inject_chars": total_chars, "rows": rows, "edits": edit_out}
