# -*- coding: utf-8 -*-
"""umo 显示名解析（导入 / 实时共用）。

优先级：umo_aliases.user_alias → auto_name → WebChat 会话 display_name → 群/私聊兜底 → umo 原文。
只读 data_v4.db（mode=ro）；拿不到（库不存在、表缺失）一律兜底不报错。缓存 60 秒。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


def parse_umo(umo: str) -> tuple[str, str, str]:
    """→ (platform_id, message_type, session)。webchat:FriendMessage:webchat!astrbot!<uuid>。"""
    parts = umo.split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return (parts[0] if parts else ""), "", umo


def _webchat_session_id(umo: str) -> str:
    # webchat 的最后一段形如 webchat!astrbot!<uuid>；platform_sessions.session_id 是最后一截 uuid
    tail = parse_umo(umo)[2]
    return tail.split("!")[-1]


def fallback_name(umo: str) -> str:
    platform_id, mtype, session = parse_umo(umo)
    if "GROUP" in mtype.upper():
        return f"群 {session}"
    if "FRIEND" in mtype.upper():
        return f"私聊 {session}"
    return umo


class NameResolver:
    def __init__(self, astrbot_db: str | Path | None = None, ttl: float = 60.0):
        self.db = str(astrbot_db) if astrbot_db else None
        self.ttl = ttl
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        self._tables: set[str] | None = None

    def _ro_conn(self) -> sqlite3.Connection | None:
        if not self.db or not Path(self.db).exists():
            return None
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                self._local.conn = conn
            except sqlite3.Error:
                self.db = None
                return None
        return conn

    def _has_table(self, name: str) -> bool:
        conn = self._ro_conn()
        if conn is None:
            return False
        if self._tables is None:
            try:
                rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                self._tables = {r["name"] for r in rows}
            except sqlite3.Error:
                self._tables = set()
        return name in self._tables

    def _query(self, umo: str) -> str | None:
        conn = self._ro_conn()
        if conn is None:
            return None
        try:
            if self._has_table("umo_aliases"):
                row = conn.execute(
                    "SELECT user_alias, auto_name FROM umo_aliases WHERE umo=?", (umo,)
                ).fetchone()
                if row and row["user_alias"]:
                    return row["user_alias"]
                if row and row["auto_name"]:
                    return row["auto_name"]
            _, mtype, _ = parse_umo(umo)
            if "FRIEND" in mtype.upper():
                sid = _webchat_session_id(umo)
                if self._has_table("platform_sessions"):
                    row = conn.execute(
                        "SELECT display_name FROM platform_sessions WHERE session_id=?", (sid,)
                    ).fetchone()
                    if row and row["display_name"]:
                        return row["display_name"]
        except sqlite3.Error:
            return None
        return None

    def resolve(self, umo: str) -> str:
        now = time.time()
        with self._lock:
            hit = self._cache.get(umo)
            if hit and now - hit[1] < self.ttl:
                return hit[0]
        name = self._query(umo) or fallback_name(umo)
        with self._lock:
            self._cache[umo] = (name, now)
        return name

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def close(self) -> None:
        with self._lock:
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None
