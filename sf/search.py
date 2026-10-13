# -*- coding: utf-8 -*-
"""全文检索：语法解析 → SQL。纯函数 + 两个入口，方便离线测试。

语法（空格分隔，多个条件取交集）：
  tool:<名字>  调用过这个工具（名字模糊匹配）
  err:          报错事件；err:<文字> 报错内容包含
  user:<文字>   用户发言包含
  umo:<片段>    限定会话
  自由词        每个词 ≥3 字走 FTS trigram，否则 LIKE

入口：
  search()            检索页：返回命中事件（每个回合最多 3 条），整库或按 umo 片段限定
  search_runs_page()  会话详情里的“搜本对话”：精确限定一个会话，按页码返回命中回合 + 每回合的命中事件
内容类型（kind）只作用于自由词和命中事件的挑选：只在这一类事件里找 / 展示。
"""

from __future__ import annotations


def parse_query(q: str) -> dict:
    """→ {tools:[], errors:[], err_any:bool, users:[], umos:[], texts:[]}。"""
    out = {"tools": [], "errors": [], "err_any": False,
           "users": [], "umos": [], "texts": []}
    for tok in (q or "").split():
        low = tok.lower()
        if low.startswith("tool:"):
            v = tok[5:].strip()
            if v:
                out["tools"].append(v)
        elif low == "err:":
            out["err_any"] = True
        elif low.startswith("err:"):
            out["errors"].append(tok[4:])
        elif low.startswith("user:"):
            v = tok[5:]
            if v:
                out["users"].append(v)
        elif low.startswith("umo:"):
            v = tok[4:]
            if v:
                out["umos"].append(v)
        else:
            out["texts"].append(tok)
    return out


def _snippet(text: str | None, needle: str, width: int = 40) -> str:
    t = text or ""
    if not needle:
        s = t[: width * 2]
        return s + ("…" if len(t) > len(s) else "")
    low = t.lower()
    i = low.find(needle.lower())
    if i < 0:
        s = t[: width * 2]
        return s + ("…" if len(t) > len(s) else "")
    a = max(0, i - width)
    b = min(len(t), i + len(needle) + width)
    s = t[a:b]
    if a > 0:
        s = "…" + s
    if b < len(t):
        s = s + "…"
    return s


def _fts_query(words: list[str]) -> str:
    # trigram 用双引号短语，多个词空格=AND（fts5 默认行为显式写 AND 更稳）
    return " AND ".join('"%s"' % w.replace('"', '""') for w in words)


def _like(s: str) -> str:
    """LIKE 的包含模式（配 ESCAPE '\\'）：反斜杠、% 、_ 都当字面量。"""
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# 页面“内容类型”下拉对应的事件种类；error 是特例（报错事件）
KIND_GROUPS = {
    "user": ("user",),
    "text": ("text", "sent"),   # 回复 = 模型原话 + 实际发出（被改写过时才有）
    "think": ("think",),
    "tool": ("tool_call", "tool_result"),
    "inject": ("inject",),
    "edit": ("edit",),         # 请求里历史 / 工具集 / 图片 / 模型被谁改了
}
KIND_ERROR = "error"


def clean_kind(kind) -> str:
    k = str(kind or "").strip()
    return k if (k in KIND_GROUPS or k == KIND_ERROR) else ""


def _kind_sql(kind: str, alias: str = "") -> tuple[str, list]:
    """事件表上的内容类型条件 → (以 AND 开头的片段, 参数)。不限类型时返回空。"""
    p = alias + "." if alias else ""
    if kind == KIND_ERROR:
        return f" AND ({p}is_error=1 OR {p}kind='error')", []
    ks = KIND_GROUPS.get(kind)
    if not ks:
        return "", []
    return f" AND {p}kind IN ({','.join('?' for _ in ks)})", list(ks)


def _scope_sql(umo_exact: str | None, alias: str = "") -> tuple[str, list]:
    """把事件限定在某个会话（精确匹配，不是包含匹配）。"""
    if not umo_exact:
        return "", []
    p = alias + "." if alias else ""
    return f" AND {p}run_id IN (SELECT run_id FROM runs WHERE umo=?)", [umo_exact]


def _has_conditions(parsed: dict, kind: str) -> bool:
    return bool(kind) or any([parsed["tools"], parsed["errors"], parsed["err_any"],
                              parsed["users"], parsed["umos"], parsed["texts"]])


def _find(store, parsed: dict, *, umo_exact: str | None = None, kind: str = "") -> list:
    """所有条件取交集 → 命中的运行 [Row(run_id, umo, s)]，新的在前（同秒按 run_id 稳定）。"""
    conn = store._read_conn()
    run_ids: set[str] | None = None

    def intersect(ids: set[str]) -> None:
        nonlocal run_ids
        run_ids = ids if run_ids is None else (run_ids & ids)

    sc, sa = _scope_sql(umo_exact)
    ks, ka = _kind_sql(kind)
    run_scope = " AND umo=?" if umo_exact else ""
    run_scope_a = [umo_exact] if umo_exact else []

    # ---- tool: 名字模糊匹配（tool_call / tool_result 的 name）
    for tool in parsed["tools"]:
        rows = conn.execute(
            "SELECT DISTINCT run_id FROM events WHERE kind IN ('tool_call','tool_result') "
            "AND name LIKE ? ESCAPE '\\'" + sc, [_like(tool)] + sa).fetchall()
        intersect({r["run_id"] for r in rows})
    # ---- err: / err:<文字>
    if parsed["err_any"]:
        rows = conn.execute("SELECT DISTINCT run_id FROM events WHERE is_error=1" + sc, sa).fetchall()
        intersect({r["run_id"] for r in rows})
    for e in parsed["errors"]:
        rows = conn.execute(
            "SELECT DISTINCT run_id FROM events WHERE is_error=1 AND text LIKE ? ESCAPE '\\'" + sc,
            [_like(e)] + sa).fetchall()
        intersect({r["run_id"] for r in rows})
    # ---- user:<文字>（user 事件 LIKE；也顺带匹配 runs.user_text，保证截断文本也能命中）
    for u in parsed["users"]:
        pat = _like(u)
        rows = conn.execute(
            "SELECT DISTINCT run_id FROM events WHERE kind='user' AND text LIKE ? ESCAPE '\\'" + sc,
            [pat] + sa).fetchall()
        ids = {r["run_id"] for r in rows}
        rows2 = conn.execute("SELECT run_id FROM runs WHERE user_text LIKE ? ESCAPE '\\'" + run_scope,
                             [pat] + run_scope_a).fetchall()
        ids |= {r["run_id"] for r in rows2}
        intersect(ids)
    # ---- umo:<片段>
    for frag in parsed["umos"]:
        rows = conn.execute("SELECT run_id FROM runs WHERE umo LIKE ? ESCAPE '\\'" + run_scope,
                            [_like(frag)] + run_scope_a).fetchall()
        intersect({r["run_id"] for r in rows})
    # ---- 自由词：≥3 字 FTS，否则 LIKE（都带上内容类型 / 会话限定）
    fts_words = [w for w in parsed["texts"] if len(w) >= 3]
    like_words = [w for w in parsed["texts"] if len(w) < 3]
    if fts_words and getattr(store, "fts", False):
        try:
            eks, eka = _kind_sql(kind, "e")
            esc, esa = _scope_sql(umo_exact, "e")
            rows = conn.execute(
                "SELECT DISTINCT e.run_id AS run_id FROM events_fts f "
                "JOIN events e ON e.id=f.rowid WHERE events_fts MATCH ?" + eks + esc,
                [_fts_query(fts_words)] + eka + esa).fetchall()
            intersect({r["run_id"] for r in rows})
        except Exception:
            like_words = parsed["texts"]  # FTS 失败全部退回 LIKE
            fts_words = []
    elif fts_words:
        like_words = parsed["texts"]
        fts_words = []
    for w in like_words:
        rows = conn.execute(
            "SELECT DISTINCT run_id FROM events WHERE text LIKE ? ESCAPE '\\'" + ks + sc,
            [_like(w)] + ka + sa).fetchall()
        intersect({r["run_id"] for r in rows})
    # ---- 只选了内容类型、没有自由词：有这一类事件的回合
    if kind and not parsed["texts"]:
        rows = conn.execute("SELECT DISTINCT run_id FROM events WHERE 1=1" + ks + sc, ka + sa).fetchall()
        intersect({r["run_id"] for r in rows})

    if not run_ids:
        return []

    # S14/S15：把命中的运行整体按时间从新到旧排好；run_id 过多时分块查，不撞 SQLite 变量上限
    CHUNK = 500   # IN (?,…) 的变量个数上限，SQLite 默认 32766，留足余量
    ids = list(run_ids)
    run_rows: list = []
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        marks = ",".join("?" for _ in chunk)
        run_rows += conn.execute(
            f"SELECT run_id, umo, COALESCE(sort_at,0) AS s FROM runs WHERE run_id IN ({marks})",
            chunk).fetchall()
    run_rows.sort(key=lambda r: (r["s"], r["run_id"]), reverse=True)
    return run_rows


def _needles(parsed: dict) -> list[str]:
    return parsed["errors"] + parsed["users"] + [w for w in parsed["texts"] if len(w) >= 1]


def _needle_for(text: str | None, needles: list[str]) -> str:
    """片段围着哪个词截：优先这段文字里真有的词，免得第二个词命中时片段截在第一个词没出现的地方。"""
    low = (text or "").lower()
    for n in needles:
        if n and n.lower() in low:
            return n
    return needles[0] if needles else ""


def _pick(events: list, lowered: list[str]) -> tuple[list, bool]:
    """events 已按「报错在前、再按 seq」排好。返回（最多 3 条要展示的, 是否还有含词事件没展示）。

    S11：含检索词的事件优先，保证展示出来的命中真含检索词，不够再拿报错 / 按 seq 补。
    """
    def match(text: str | None) -> bool:
        t = (text or "").lower()
        return any(n in t for n in lowered) if lowered else False

    matched = [e for e in events if match(e["text"])]
    picked = matched[:3]
    if len(picked) < 3:
        seen = {id(e) for e in picked}
        # 已有含词事件时，只补报错事件，别把含词的挤掉；一条含词的都没有时才补任意事件
        fill = [e for e in events if id(e) not in seen and (e["is_error"] if matched else True)]
        picked += fill[:3 - len(picked)]
    return picked, len(matched) > 3


def _events_of(conn, run_ids: list[str], kind: str = "") -> dict[str, list]:
    """一批运行的事件（可只取某一类），每个运行内：报错在前、再按 seq。"""
    ks, ka = _kind_sql(kind)
    marks = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        "SELECT run_id, seq, kind, name, text, t, is_error FROM events "
        f"WHERE run_id IN ({marks}){ks} ORDER BY run_id, CASE WHEN is_error=1 THEN 0 ELSE 1 END, seq",
        list(run_ids) + ka).fetchall()
    by_run: dict[str, list] = {}
    for e in rows:
        by_run.setdefault(e["run_id"], []).append(e)
    return by_run


def search(store, names, q: str, *, umo: str | None = None,
           limit: int = 50) -> dict:
    """返回 {hits:[{run_id,umo,display_name,kind,name,snippet,t,is_error,seq}], truncated}。"""
    limit = max(1, min(int(limit or 50), 200))
    parsed = parse_query(q)
    if umo:
        parsed["umos"].append(umo)
    if not _has_conditions(parsed, ""):
        return {"hits": [], "truncated": False}
    run_rows = _find(store, parsed)
    if not run_rows:
        return {"hits": [], "truncated": False}

    # S14/S15：一小批一小批地取事件，凑够 limit 条命中就停——不用把全部命中运行的事件都读一遍
    BATCH = 50
    needles = _needles(parsed)
    lowered = [n.lower() for n in needles]
    conn = store._read_conn()
    hits: list[dict] = []
    truncated = False
    for i in range(0, len(run_rows), BATCH):
        batch = run_rows[i:i + BATCH]
        by_run = _events_of(conn, [r["run_id"] for r in batch])
        for r in batch:
            picked, more = _pick(by_run.get(r["run_id"], []), lowered)
            truncated = truncated or more
            for e in picked:
                hits.append({
                    "run_id": r["run_id"], "umo": r["umo"], "display_name": names.resolve(r["umo"]),
                    "kind": e["kind"], "name": e["name"], "seq": e["seq"],
                    "snippet": _snippet(e["text"], _needle_for(e["text"], needles)),
                    "t": e["t"], "is_error": bool(e["is_error"]),
                })
        if len(hits) >= limit:
            truncated = truncated or len(hits) > limit or i + BATCH < len(run_rows)
            hits = hits[:limit]
            break
    return {"hits": hits, "truncated": truncated}


def search_runs_page(store, q: str, *, umo: str, kind: str = "",
                     page: int = 1, size: int = 20) -> dict:
    """会话详情里“搜本对话”：umo 精确限定。

    返回 {total, pages, page, size, ids:[本页的 run_id，新的在前], hits:{run_id:[事件…]}, more:[还有命中没展示的 run_id]}。
    page 超出范围时收到最后一页。q 和 kind 都没有有效条件时返回 0 条。
    """
    kind = clean_kind(kind)
    size = max(1, min(int(size or 20), 200))
    parsed = parse_query(q)
    out = {"total": 0, "pages": 1, "page": 1, "size": size, "ids": [], "hits": {}, "more": []}
    if not umo or not _has_conditions(parsed, kind):
        return out
    rows = _find(store, parsed, umo_exact=umo, kind=kind)
    total = len(rows)
    pages = max(1, -(-total // size))
    page = max(1, min(int(page or 1), pages))
    ids = [r["run_id"] for r in rows[(page - 1) * size: page * size]]
    out.update(total=total, pages=pages, page=page, ids=ids)
    if not ids:
        return out
    needles = _needles(parsed)
    lowered = [n.lower() for n in needles]
    by_run = _events_of(store._read_conn(), ids, kind)
    for rid in ids:
        picked, more = _pick(by_run.get(rid, []), lowered)
        picked.sort(key=lambda e: e["seq"])  # 卡片里按发生顺序展示
        out["hits"][rid] = [{
            "seq": e["seq"], "kind": e["kind"], "name": e["name"],
            "snippet": _snippet(e["text"], _needle_for(e["text"], needles), 60),
            "t": e["t"], "is_error": bool(e["is_error"]),
        } for e in picked]
        if more:
            out["more"].append(rid)
    return out
