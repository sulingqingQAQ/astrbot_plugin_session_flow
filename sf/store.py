# -*- coding: utf-8 -*-
"""会话流存储层：SQLite（WAL），同步 API，调用方用 asyncio.to_thread 包。

线程模型：一个写连接（写事务串行，写方法内部加锁），查询走独立的只读连接。
所有时间都是 epoch 秒（float），旧档允许为空。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = "1"

RUN_COLUMNS = [
    "run_id", "umo", "cid", "source", "status", "sort_at", "ord", "time_source",
    "sender_id", "sender_name", "msg_at", "received_at", "prep_at", "begin_at",
    "end_at", "provider_id", "model", "user_text", "reply_text", "error_text",
    "sysprompt_hash", "contexts_len", "tools_available", "n_steps", "n_tools",
    "n_errors", "token_in", "token_cached", "token_out", "ttft", "stats_id",
]

# 列表页不取的大字段（正文三件另用 substr 取 300 字预览，见 list_runs_page）
RUN_LIST_SKIP = {"user_text", "reply_text", "error_text", "tools_available"}


class Store:
    def __init__(self, db_path: str | os.PathLike, *, astrbot_db: str | os.PathLike | None = None):
        self.path = Path(db_path)
        self.astrbot_db = str(astrbot_db) if astrbot_db else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wlock = threading.RLock()
        self._w = sqlite3.connect(str(self.path), check_same_thread=False)
        self._w.row_factory = sqlite3.Row
        self._w.execute("PRAGMA journal_mode=WAL")
        self._w.execute("PRAGMA synchronous=NORMAL")
        self._w.execute("PRAGMA temp_store=MEMORY")
        self._w.execute("PRAGMA busy_timeout=5000")
        self.fts = True
        self._init_schema()
        # S6：读连接按线程隔离（webapi 在 to_thread 里读库，不能多线程共用一个连接）
        self._local = threading.local()

    # ------------------------------------------------------------ schema
    def _read_conn(self) -> sqlite3.Connection:
        # 只读连接：uri mode=ro，保证对数据文件只读；每个线程一个
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._wlock, self._w:
            self._w.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS blobs (
                    hash TEXT PRIMARY KEY, size INTEGER NOT NULL,
                    data BLOB NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS umos (
                    umo TEXT PRIMARY KEY, platform_id TEXT, message_type TEXT,
                    display_name TEXT, first_at REAL, last_at REAL,
                    run_count INTEGER DEFAULT 0, tool_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0, legacy_count INTEGER DEFAULT 0,
                    token_in INTEGER DEFAULT 0, token_out INTEGER DEFAULT 0,
                    last_user TEXT, last_reply TEXT, last_status TEXT, last_cid TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, umo TEXT NOT NULL, cid TEXT,
                    source TEXT NOT NULL, status TEXT NOT NULL,
                    sort_at REAL, ord INTEGER DEFAULT 0, time_source TEXT,
                    sender_id TEXT, sender_name TEXT,
                    msg_at REAL, received_at REAL, prep_at REAL, begin_at REAL, end_at REAL,
                    provider_id TEXT, model TEXT,
                    user_text TEXT, reply_text TEXT, error_text TEXT,
                    sysprompt_hash TEXT, contexts_len INTEGER, tools_available TEXT,
                    n_steps INTEGER DEFAULT 0, n_tools INTEGER DEFAULT 0, n_errors INTEGER DEFAULT 0,
                    token_in INTEGER DEFAULT 0, token_cached INTEGER DEFAULT 0,
                    token_out INTEGER DEFAULT 0, ttft REAL, stats_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS ix_runs_umo_sort ON runs(umo, sort_at, ord);
                CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status);
                CREATE INDEX IF NOT EXISTS ix_runs_cid ON runs(cid);
                CREATE INDEX IF NOT EXISTS ix_runs_stats ON runs(stats_id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    t REAL, kind TEXT NOT NULL, step INTEGER DEFAULT 1,
                    name TEXT, call_id TEXT, text TEXT,
                    data TEXT, is_error INTEGER DEFAULT 0, dur_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, seq);
                CREATE INDEX IF NOT EXISTS ix_events_kind_name ON events(kind, name);
                -- S12：runs 页每轮查 tools_used（run_id=? AND kind='tool_call'），
                -- 没有统计信息时优化器会选 kind 索引扫全表，这个覆盖索引让它稳定走 run_id
                CREATE INDEX IF NOT EXISTS ix_events_run_kind_name ON events(run_id, kind, name);
                """
            )
            # FTS5 trigram；建不出来就退回 LIKE（fts=False）
            try:
                self._w.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                        text, content='events', content_rowid='id', tokenize='trigram'
                    );
                    CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
                        INSERT INTO events_fts(rowid, text) VALUES (new.id, new.text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
                        INSERT INTO events_fts(events_fts, rowid, text) VALUES ('delete', old.id, old.text);
                    END;
                    """
                )
                self.fts = True
            except sqlite3.Error:
                self.fts = False
            old = self.get_meta("schema_version")
            if not old:
                self.set_meta("schema_version", SCHEMA_VERSION)

    # ------------------------------------------------------------ meta / blob
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._wlock:
            row = self._w.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._wlock, self._w:
            self._w.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def delete_meta_prefix(self, prefix: str) -> int:
        with self._wlock, self._w:
            cur = self._w.execute("DELETE FROM meta WHERE key LIKE ?", (prefix + "%",))
            return cur.rowcount

    def put_blob(self, hash_: str, text: str) -> int:
        """存 system prompt 文本（zlib 压缩，按哈希去重）。返回 UTF-8 字节数。"""
        raw = text.encode("utf-8", "replace")
        blob = zlib.compress(raw, 6)
        with self._wlock, self._w:
            self._w.execute(
                "INSERT OR IGNORE INTO blobs(hash,size,data,created_at) VALUES(?,?,?,?)",
                (hash_, len(raw), blob, time.time()),
            )
        return len(raw)

    def get_blob(self, hash_: str) -> dict | None:
        conn = self._read_conn()
        row = conn.execute("SELECT hash,size,data FROM blobs WHERE hash=?", (hash_,)).fetchone()
        if not row:
            return None
        return {"hash": row["hash"], "size": row["size"], "text": zlib.decompress(row["data"]).decode("utf-8", "replace")}

    def blob_hashes(self) -> set[str]:
        conn = self._read_conn()
        return {r[0] for r in conn.execute("SELECT hash FROM blobs")}

    # ------------------------------------------------------------ umo 卡
    def touch_umo(self, umo: str, platform_id: str, message_type: str, display_name: str | None) -> None:
        """确保卡片行存在并记下身份字段；聚合值只由 recompute_umo 维护。"""
        with self._wlock, self._w:
            self._w.execute(
                "INSERT INTO umos(umo,platform_id,message_type,display_name) VALUES(?,?,?,?) "
                "ON CONFLICT(umo) DO UPDATE SET "
                "platform_id=COALESCE(excluded.platform_id, umos.platform_id), "
                "message_type=COALESCE(excluded.message_type, umos.message_type), "
                "display_name=COALESCE(umos.display_name, excluded.display_name)",
                (umo, platform_id, message_type, display_name),
            )

    def set_display_name(self, umo: str, name: str) -> None:
        with self._wlock, self._w:
            self._w.execute("UPDATE umos SET display_name=? WHERE umo=?", (name, umo))

    def recompute_umo(self, umo: str) -> None:
        """按 runs/events 全量重算一张卡（每次运行写完、旧档导入后调用）。"""
        with self._wlock, self._w:
            row = self._w.execute(
                """
                SELECT COUNT(*) c, COALESCE(SUM(CASE WHEN source='legacy' THEN 1 ELSE 0 END),0) legacy,
                       COALESCE(SUM(CASE WHEN status IN ('error','interrupted') OR n_errors>0 THEN 1 ELSE 0 END),0) errors,
                       COALESCE(SUM(n_tools),0) tools, COALESCE(SUM(token_in),0) tin,
                       COALESCE(SUM(token_out),0) tout,
                       MIN(COALESCE(sort_at,0)) first_at, MAX(COALESCE(sort_at,0)) last_at
                FROM runs WHERE umo=?
                """,
                (umo,),
            ).fetchone()
            if not row or row["c"] == 0:
                self._w.execute("DELETE FROM umos WHERE umo=?", (umo,))
                return
            last = self._w.execute(
                "SELECT sort_at, user_text, reply_text, status, cid FROM runs "
                "WHERE umo=? ORDER BY COALESCE(sort_at,0) DESC, ord DESC LIMIT 1",
                (umo,),
            ).fetchone()
            cur = self._w.execute(
                "SELECT platform_id,message_type,display_name FROM umos WHERE umo=?", (umo,)
            ).fetchone()
            self._w.execute(
                """
                INSERT INTO umos(umo,platform_id,message_type,display_name,first_at,last_at,
                    run_count,tool_count,error_count,legacy_count,token_in,token_out,
                    last_user,last_reply,last_status,last_cid)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(umo) DO UPDATE SET
                    first_at=excluded.first_at, last_at=excluded.last_at,
                    run_count=excluded.run_count, tool_count=excluded.tool_count,
                    error_count=excluded.error_count, legacy_count=excluded.legacy_count,
                    token_in=excluded.token_in, token_out=excluded.token_out,
                    last_user=excluded.last_user, last_reply=excluded.last_reply,
                    last_status=excluded.last_status, last_cid=excluded.last_cid
                """,
                (
                    umo, cur["platform_id"] if cur else None, cur["message_type"] if cur else None,
                    cur["display_name"] if cur else None,
                    row["first_at"] or None, row["last_at"] or None,
                    row["c"], row["tools"], row["errors"], row["legacy"],
                    row["tin"], row["tout"],
                    (last["user_text"] or "")[:500], (last["reply_text"] or "")[:500],
                    last["status"], last["cid"],
                ),
            )

    def recompute_umos(self, umos: Iterable[str]) -> None:
        for umo in set(umos):
            self.recompute_umo(umo)

    # ------------------------------------------------------------ runs
    def insert_run(self, fields: dict[str, Any]) -> None:
        cols = [c for c in RUN_COLUMNS if c in fields]
        placeholders = ",".join("?" for _ in cols)
        vals: list[Any] = []
        for c in cols:
            v = fields[c]
            if c == "tools_available" and v is not None and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False)
            vals.append(v)
        with self._wlock, self._w:
            self._w.execute(
                f"INSERT INTO runs({','.join(cols)}) VALUES({placeholders})", vals
            )

    def update_run(self, run_id: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        sets, vals = [], []
        for c, v in fields.items():
            if c not in RUN_COLUMNS:
                continue
            if c == "tools_available" and v is not None and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{c}=?")
            vals.append(v)
        vals.append(run_id)
        with self._wlock, self._w:
            self._w.execute(f"UPDATE runs SET {','.join(sets)} WHERE run_id=?", vals)

    def get_run_row(self, run_id: str) -> sqlite3.Row | None:
        return self._read_conn().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def running_run_ids_for_umo(self, umo: str, exclude: str | None = None) -> list[str]:
        conn = self._read_conn()
        rows = conn.execute(
            "SELECT run_id FROM runs WHERE umo=? AND status='running'", (umo,)
        ).fetchall()
        return [r["run_id"] for r in rows if r["run_id"] != exclude]

    def all_running(self) -> list[sqlite3.Row]:
        return list(self._read_conn().execute("SELECT * FROM runs WHERE status='running'").fetchall())

    def mark_interrupted(self, run_ids: Sequence[str], at: float | None = None) -> int:
        if not run_ids:
            return 0
        at = at if at is not None else time.time()
        n = 0
        ids = list(run_ids)
        with self._wlock, self._w:
            for i in range(0, len(ids), self._IN_CHUNK):
                chunk = ids[i:i + self._IN_CHUNK]
                cur = self._w.execute(
                    "UPDATE runs SET status='interrupted', end_at=COALESCE(end_at,?) "
                    f"WHERE run_id IN ({','.join('?' for _ in chunk)}) AND status='running'",
                    [at, *chunk],
                )
                n += cur.rowcount
        return n

    def runs_for_cid(self, cid: str, source: str | None = None) -> list[sqlite3.Row]:
        conn = self._read_conn()
        sql = "SELECT * FROM runs WHERE cid=?"
        args: list[Any] = [cid]
        if source:
            sql += " AND source=?"
            args.append(source)
        return list(conn.execute(sql, args).fetchall())

    # S7：IN (?,…) 按块分批，SQLite 变量上限（本机 32766）下大批量不抛错
    _IN_CHUNK = 500

    def delete_runs(self, run_ids: Sequence[str]) -> int:
        if not run_ids:
            return 0
        n = 0
        ids = list(run_ids)
        with self._wlock, self._w:
            for i in range(0, len(ids), self._IN_CHUNK):
                chunk = ids[i:i + self._IN_CHUNK]
                marks = ",".join("?" for _ in chunk)
                self._w.execute(f"DELETE FROM events WHERE run_id IN ({marks})", chunk)
                cur = self._w.execute(f"DELETE FROM runs WHERE run_id IN ({marks})", chunk)
                n += cur.rowcount
        return n

    def delete_cid_runs(self, cid: str, source: str = "legacy") -> int:
        with self._wlock:
            rows = self._w.execute(
                "SELECT run_id FROM runs WHERE cid=? AND source=?", (cid, source)
            ).fetchall()
        return self.delete_runs([r["run_id"] for r in rows])

    # ------------------------------------------------------------ events
    def insert_events(self, events: Sequence[dict[str, Any]]) -> None:
        """events: {run_id,seq,t,kind,step,name,call_id,text,data(dict),is_error,dur_ms}"""
        if not events:
            return
        rows = []
        for e in events:
            data = e.get("data")
            if data is not None and not isinstance(data, str):
                data = json.dumps(data, ensure_ascii=False)
            rows.append((
                e["run_id"], e["seq"], e.get("t"), e["kind"], e.get("step", 1),
                e.get("name"), e.get("call_id"), e.get("text"), data,
                1 if e.get("is_error") else 0, e.get("dur_ms"),
            ))
        with self._wlock, self._w:
            self._w.executemany(
                "INSERT INTO events(run_id,seq,t,kind,step,name,call_id,text,data,is_error,dur_ms)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def events_for(self, run_id: str) -> list[sqlite3.Row]:
        return list(self._read_conn().execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY seq,id", (run_id,)
        ).fetchall())

    def max_event_seq(self, run_id: str) -> int:
        """这个运行里最大的事件序号（给已结束的运行补事件用）。"""
        return self.max_error_event_seq(run_id)

    def max_error_event_seq(self, run_id: str) -> int:
        row = self._read_conn().execute(
            "SELECT COALESCE(MAX(seq),0) m FROM events WHERE run_id=?", (run_id,)
        ).fetchone()
        return int(row["m"])

    # ------------------------------------------------------------ 查询
    @staticmethod
    def row_to_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("tools_available"), str):
            try:
                d["tools_available"] = json.loads(d["tools_available"])
            except json.JSONDecodeError:
                pass
        return d

    def db_bytes(self) -> int:
        total = self.path.stat().st_size
        for suffix in ("-wal", "-shm"):
            p = self.path.with_name(self.path.name + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def admin_stats(self) -> dict[str, Any]:
        conn = self._read_conn()
        r = conn.execute(
            "SELECT COUNT(*) runs, MIN(COALESCE(sort_at,0)) oldest, MAX(COALESCE(sort_at,0)) newest FROM runs"
        ).fetchone()
        e = conn.execute("SELECT COUNT(*) c FROM events").fetchone()
        b = conn.execute("SELECT COUNT(*) c FROM blobs").fetchone()
        return {
            "db_bytes": self.db_bytes(),
            "runs": r["runs"],
            "events": e["c"],
            "blobs": b["c"],
            "oldest_at": r["oldest"] or None,
            "newest_at": r["newest"] or None,
            "fts": self.fts,
        }

    def global_stats(self) -> dict[str, Any]:
        conn = self._read_conn()
        r = conn.execute(
            """SELECT COUNT(*) runs,
               COALESCE(SUM(CASE WHEN source='legacy' THEN 1 ELSE 0 END),0) legacy_runs,
               COALESCE(SUM(CASE WHEN status='running' THEN 1 ELSE 0 END),0) running,
               COALESCE(SUM(CASE WHEN status IN ('error','interrupted') OR n_errors>0 THEN 1 ELSE 0 END),0) errors,
               COALESCE(SUM(n_tools),0) tools
               FROM runs"""
        ).fetchone()
        u = conn.execute("SELECT COUNT(*) c FROM umos").fetchone()
        return {
            "umos": u["c"], "runs": r["runs"], "tools": r["tools"],
            "errors": r["errors"], "running": r["running"], "legacy_runs": r["legacy_runs"],
            "db_bytes": self.db_bytes(),
        }

    def platforms(self) -> list[dict]:
        return [dict(r) for r in self._read_conn().execute(
            "SELECT platform_id, COUNT(*) c FROM umos GROUP BY platform_id ORDER BY c DESC"
        ).fetchall()]

    def tool_top(self, limit: int = 10) -> list[dict]:
        # S3：报错数按 tool_result 的 is_error 算（tool_call 行上没有报错标记）
        return [dict(r) for r in self._read_conn().execute(
            "SELECT n.name AS name, n.c AS count, COALESCE(e.err,0) AS errors FROM "
            "(SELECT name, COUNT(*) c FROM events WHERE kind='tool_call' AND name IS NOT NULL GROUP BY name) n "
            "LEFT JOIN (SELECT name, SUM(is_error) err FROM events "
            "WHERE kind='tool_result' AND name IS NOT NULL GROUP BY name) e ON e.name=n.name "
            "ORDER BY n.c DESC LIMIT ?", (limit,)
        ).fetchall()]

    def list_umos(
        self, *, type_: str = "all", platform: str | None = None,
        errors_only: bool = False, sort: str = "recent", q: str | None = None,
    ) -> list[dict]:
        where, args = [], []
        if platform:
            where.append("u.platform_id=?")
            args.append(platform)
        if errors_only:
            where.append("u.error_count>0")
        if type_ == "group":
            where.append("u.message_type LIKE '%GROUP%'")
        elif type_ == "private":
            where.append("u.message_type LIKE '%FRIEND%'")
        elif type_ == "other":
            where.append("(u.message_type NOT LIKE '%GROUP%' AND u.message_type NOT LIKE '%FRIEND%')")
        if q:
            where.append("(u.display_name LIKE ? OR u.umo LIKE ? OR u.last_user LIKE ?)")
            like = f"%{q}%"
            args += [like, like, like]
        order = {
            "runs": "u.run_count DESC",
            "errors": "u.error_count DESC, u.last_at DESC",
            "tokens": "u.token_in DESC",
        }.get(sort, "u.last_at DESC")
        sql = "SELECT u.* FROM umos u"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order}"
        return [dict(r) for r in self._read_conn().execute(sql, args).fetchall()]

    def list_runs_page(
        self, umo: str, *, before: tuple | None = None, limit: int = 50
    ) -> tuple[list[dict], bool, str | None]:
        """返回 (升序的本页 runs, has_more, next_before 不透明游标)。

        before=(sort_at, ord) 或 (sort_at, ord, rowid)，向更早翻。
        S8：同秒多轮 (sort_at, ord) 会撞车，rowid 做最终决胜，翻页不丢行。
        """
        conn = self._read_conn()
        where, args = "umo=?", [umo]
        if before is not None:
            s = before[0]
            o = before[1] if len(before) > 1 else 0
            where += " AND (COALESCE(sort_at,0) < ? OR (COALESCE(sort_at,0)=? AND ord < ?)"
            args += [s, s, o]
            if len(before) > 2 and before[2] is not None:
                where += " OR (COALESCE(sort_at,0)=? AND ord=? AND rowid<?)"
                args += [s, o, before[2]]
            where += ")"
        cols = [c for c in RUN_COLUMNS if c not in RUN_LIST_SKIP]
        cols += ["substr(user_text,1,300) AS user_text",
                 "substr(reply_text,1,300) AS reply_text",
                 "substr(error_text,1,300) AS error_text",
                 "rowid AS _rowid"]
        rows = list(conn.execute(
            f"SELECT {','.join(cols)} FROM runs WHERE {where} "
            "ORDER BY COALESCE(sort_at,0) DESC, ord DESC, rowid DESC LIMIT ?",
            args + [limit + 1],
        ).fetchall())
        has_more = len(rows) > limit
        rows = rows[:limit]
        out = []
        for r in reversed(rows):  # 升序给前端
            d = self.row_to_dict(r)
            assert d is not None
            out.append(d)
        next_before = None
        if out:
            first = out[0]
            next_before = f"{first.get('sort_at') or 0}:{first.get('ord') or 0}:{first.get('_rowid') or 0}"
        return out, has_more, next_before

    @staticmethod
    def _list_cols() -> str:
        cols = [c for c in RUN_COLUMNS if c not in RUN_LIST_SKIP]
        cols += ["substr(user_text,1,300) AS user_text",
                 "substr(reply_text,1,300) AS reply_text",
                 "substr(error_text,1,300) AS error_text",
                 "rowid AS _rowid"]
        return ",".join(cols)

    def count_runs(self, umo: str) -> int:
        r = self._read_conn().execute("SELECT COUNT(*) AS c FROM runs WHERE umo=?", (umo,)).fetchone()
        return int(r["c"]) if r else 0

    def list_runs_offset(self, umo: str, *, offset: int = 0, limit: int = 20) -> list[dict]:
        """按页码取：新的在前（和翻页方向一致）。页间有新回合进来时会错位一两条，不丢也不重复太多。"""
        rows = self._read_conn().execute(
            f"SELECT {self._list_cols()} FROM runs WHERE umo=? "
            "ORDER BY COALESCE(sort_at,0) DESC, ord DESC, rowid DESC LIMIT ? OFFSET ?",
            (umo, max(1, int(limit)), max(0, int(offset)))).fetchall()
        return [self.row_to_dict(r) for r in rows]  # type: ignore[misc]

    def runs_by_ids(self, run_ids: Sequence[str]) -> list[dict]:
        """按给定顺序取回合（同列表页的轻量列）；库里没有的跳过。"""
        ids = list(run_ids)
        if not ids:
            return []
        conn = self._read_conn()
        got: dict[str, dict] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in conn.execute(
                    f"SELECT {self._list_cols()} FROM runs WHERE run_id IN ({marks})", chunk).fetchall():
                d = self.row_to_dict(r)
                if d is not None:
                    got[d["run_id"]] = d
        return [got[i] for i in ids if i in got]

    def run_detail(self, run_id: str) -> dict | None:
        return self.row_to_dict(self.get_run_row(run_id))

    def prev_run(self, umo: str, sort_at: float | None, ord_: int) -> sqlite3.Row | None:
        """同 umo 里时间上一条运行（用于 system prompt 是否同上一轮）。"""
        conn = self._read_conn()
        if sort_at is None:
            return conn.execute(
                "SELECT * FROM runs WHERE umo=? AND sort_at IS NULL AND ord<? "
                "ORDER BY ord DESC LIMIT 1", (umo, ord_)
            ).fetchone()
        return conn.execute(
            "SELECT * FROM runs WHERE umo=? AND (COALESCE(sort_at,0)<? "
            "OR (COALESCE(sort_at,0)=? AND ord<?)) ORDER BY COALESCE(sort_at,0) DESC, ord DESC LIMIT 1",
            (umo, sort_at, sort_at, ord_),
        ).fetchone()

    # ------------------------------------------------------------ 清理
    def purge(self, days: int) -> int:
        """删 sort_at 早于 N 天前的运行；顺带删孤立 blob。返回删除行数。"""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        with self._wlock, self._w:
            rows = self._w.execute(
                "SELECT run_id, umo FROM runs WHERE sort_at IS NOT NULL AND sort_at<?", (cutoff,)
            ).fetchall()
            ids = [r["run_id"] for r in rows]
            umos = {r["umo"] for r in rows}
        if not ids:
            return 0
        self.delete_runs(ids)
        self._prune_blobs()
        self.recompute_umos(umos)
        with self._wlock, self._w:
            self._w.execute("VACUUM")
        return len(ids)

    def _prune_blobs(self) -> int:
        with self._wlock:
            used = {r[0] for r in self._w.execute(
                "SELECT DISTINCT sysprompt_hash FROM runs WHERE sysprompt_hash IS NOT NULL"
            ).fetchall()}
            stale = [h for h in self.blob_hashes() if h not in used]
        if stale:
            with self._wlock, self._w:
                for i in range(0, len(stale), self._IN_CHUNK):
                    chunk = stale[i:i + self._IN_CHUNK]
                    marks = ",".join("?" for _ in chunk)
                    self._w.execute(f"DELETE FROM blobs WHERE hash IN ({marks})", chunk)
        return len(stale)

    def close(self) -> None:
        with self._wlock:
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None
            self._w.close()
