# -*- coding: utf-8 -*-
"""规则摘要：纯 SQL 统计，不调 LLM。"""

from __future__ import annotations


def umo_summary(store, umo: str) -> dict:
    conn = store._read_conn()
    r = conn.execute(
        "SELECT COUNT(*) n, MIN(sort_at) first_at, MAX(sort_at) last_at, "
        "COALESCE(SUM(token_in),0) ti, COALESCE(SUM(token_out),0) tout, "
        "COALESCE(SUM(CASE WHEN source='legacy' THEN 1 ELSE 0 END),0) legacy_n, "
        "COALESCE(SUM(CASE WHEN status IN ('error','interrupted') OR n_errors>0 THEN 1 ELSE 0 END),0) err_n "
        "FROM runs WHERE umo=?", (umo,)).fetchone()
    cids = conn.execute(
        "SELECT cid, COUNT(*) n, MIN(sort_at) first_at, MAX(sort_at) last_at "
        "FROM runs WHERE umo=? GROUP BY cid ORDER BY first_at", (umo,)).fetchall()
    tools = conn.execute(
        "SELECT c.name, COUNT(*) n, COALESCE(e.err,0) err FROM "
        "(SELECT e2.run_id, e2.name FROM events e2 JOIN runs r2 ON r2.run_id=e2.run_id "
        "WHERE r2.umo=? AND e2.kind='tool_call') c "
        "LEFT JOIN (SELECT e3.name, SUM(e3.is_error) err FROM events e3 "
        "JOIN runs r3 ON r3.run_id=e3.run_id "
        "WHERE r3.umo=? AND e3.kind='tool_result' GROUP BY e3.name) e ON e.name=c.name "
        "GROUP BY c.name ORDER BY n DESC LIMIT 5", (umo, umo)).fetchall()
    last_err = conn.execute(
        "SELECT e.t, e.name, e.text, r.sort_at FROM events e "
        "JOIN runs r ON r.run_id=e.run_id "
        "WHERE r.umo=? AND e.is_error=1 ORDER BY COALESCE(e.t, r.sort_at) DESC LIMIT 1",
        (umo,)).fetchone()
    first = conn.execute(
        "SELECT user_text, sort_at FROM runs WHERE umo=? ORDER BY sort_at ASC, ord ASC LIMIT 1",
        (umo,)).fetchone()
    last = conn.execute(
        "SELECT reply_text, sort_at FROM runs WHERE umo=? ORDER BY sort_at DESC, ord DESC LIMIT 1",
        (umo,)).fetchone()
    return {
        "umo": umo,
        "runs": r["n"], "legacy_runs": r["legacy_n"], "errors": r["err_n"],
        "first_at": r["first_at"], "last_at": r["last_at"],
        "token_in": r["ti"], "token_out": r["tout"],
        "cids": [{"cid": c["cid"], "runs": c["n"],
                  "first_at": c["first_at"], "last_at": c["last_at"]} for c in cids],
        "tool_top": [{"name": t["name"], "count": t["n"], "errors": t["err"]} for t in tools],
        "last_error": ({"t": last_err["t"] or last_err["sort_at"], "name": last_err["name"],
                        "text": (last_err["text"] or "")[:200]} if last_err else None),
        "first_task": ((first["user_text"] or "")[:200] if first else ""),
        "last_reply": ((last["reply_text"] or "")[:300] if last else ""),
    }
