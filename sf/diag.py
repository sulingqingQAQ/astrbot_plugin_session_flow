# -*- coding: utf-8 -*-
"""诊断日志与导出：让用户能把“插件到底跑成什么样”导出来给别人看。

DiagLog：包装宿主 logger——照样转给宿主，同时记进「内存环 + 滚动日志文件」，
并统计各钩子的次数 / 耗时 / 报错、留一条最近钩子轨迹。
build_bundle：把环境、配置、库状态、运行时状态、钩子统计、最近报错、最近运行摘要、
日志尾巴合成一个 JSON，供插件页「下载诊断包」。

隐私：日志和诊断包默认**不含任何聊天正文**（只有运行 id、状态、数量、耗时），
会话 ID 默认打码成 平台:类型:#哈希；用户勾选「保留原始会话 ID」才附一张对照表。
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import platform
import re
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

FORMAT = "session_flow_diag/1"
PREFIX = "[session_flow] "
SLOW_HOOK_MS = 200.0

_SECRET_KEY = re.compile(r"key|token|secret|passw|cookie|auth", re.I)


# =================================================================== 工具

def mask_umo(umo: str | None) -> str:
    """平台:类型:#6位哈希。哈希稳定，同一会话每次导出都一样。"""
    s = str(umo or "")
    if not s:
        return ""
    parts = s.split(":", 2)
    head = ":".join(parts[:2]) if len(parts) >= 2 else parts[0]
    return f"{head}:#{hashlib.sha1(s.encode('utf-8')).hexdigest()[:6]}"


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _clip(text: Any, n: int) -> str:
    t = text if isinstance(text, str) else str(text or "")
    return t if len(t) <= n else t[:n] + "…"


def _redact_home(text: str) -> str:
    """路径里的用户主目录换成 ~，免得把系统用户名带出去。"""
    try:
        home = str(Path.home())
        if home and len(home) > 3:
            return text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    except Exception:
        pass
    return text


# =================================================================== 钩子守卫

class _Guard:
    """with diag.guard("agent_done", umo): ...  计时；Exception 记下来并吞掉（钩子绝不外抛）。"""

    __slots__ = ("diag", "name", "umo", "t0")

    def __init__(self, diag: "DiagLog", name: str, umo: str):
        self.diag = diag
        self.name = name
        self.umo = umo
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, et, ev, tb):
        ms = (time.perf_counter() - self.t0) * 1000.0
        self.diag._hook_done(self.name, self.umo, ms, et, ev, tb)
        return et is not None and issubclass(et, Exception)


# =================================================================== 滚动文件

class _RotatingFile:
    """按大小滚动的追加写文件。不用 logging 模块，也就不会多出一个 Logger 实例。

    句柄常开、每行写完 flush（崩溃前最后几行也在盘上）；写满 max_bytes 就把 .1 → .2 → … → .N
    依次改名，最老的那份丢掉。
    Windows 下文件可能被编辑器 / 杀毒软件占着，改名会失败：这一回就不滚，继续往当前文件里追加，
    原因记在 rotate_error；只有文件长到 max_bytes 的 HARD_FACTOR 倍还滚不动，才把它清空重写，
    免得无限长大。
    """

    HARD_FACTOR = 4

    def __init__(self, path: str, max_bytes: int, backups: int):
        self.path = str(path)
        self.max_bytes = max(1, int(max_bytes))
        self.backups = max(0, int(backups))
        self.rotate_error: str | None = None
        self._lock = threading.Lock()
        self._fh = None
        self._size = 0
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def write_line(self, line: str) -> None:
        data = (line + "\n").encode("utf-8", "replace")
        with self._lock:
            if self._fh is None:
                self._open("ab")
            if self._size > 0 and self._size + len(data) > self.max_bytes:
                self._rotate()
            self._fh.write(data)
            self._fh.flush()
            self._size += len(data)

    def close(self) -> None:
        with self._lock:
            self._close_fh()

    # ---- 以下都在 _lock 里调用
    def _open(self, mode: str) -> None:
        self._fh = open(self.path, mode)
        self._size = os.path.getsize(self.path)

    def _close_fh(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def _rotate(self) -> None:
        self._close_fh()
        try:
            if self.backups > 0:
                for i in range(self.backups - 1, 0, -1):
                    src = f"{self.path}.{i}"
                    if os.path.exists(src):
                        os.replace(src, f"{self.path}.{i + 1}")
                os.replace(self.path, f"{self.path}.1")
            else:
                self._open("wb")      # 不留旧文件：直接清空
                return
            self.rotate_error = None
            self._open("ab")
        except OSError as exc:
            self.rotate_error = f"{type(exc).__name__}: {exc}"
            self._close_fh()
            try:
                too_big = os.path.getsize(self.path) > self.max_bytes * self.HARD_FACTOR
            except OSError:
                too_big = False
            self._open("wb" if too_big else "ab")


# =================================================================== DiagLog

class DiagLog:
    def __init__(self, host: Any = None, *, file_path: str | os.PathLike | None = None,
                 ring_size: int = 2000, max_bytes: int = 1_000_000, backups: int = 3,
                 enabled: bool = True, log_runs: bool = True,
                 clock: Callable[[], float] = time.time):
        self.host = host
        self.enabled = enabled
        self.log_runs = log_runs
        self.clock = clock
        self.started_at = clock()
        self.file_path = str(file_path) if file_path else None
        self.file_error: str | None = None
        self.error_count = 0
        self.ring: collections.deque[str] = collections.deque(maxlen=ring_size)
        self.errors: collections.deque[dict] = collections.deque(maxlen=50)
        self.trace: collections.deque[tuple] = collections.deque(maxlen=300)
        self.hooks: dict[str, dict] = {}
        self.notes: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._slow_logged: dict[str, float] = {}
        self._file: _RotatingFile | None = None
        if enabled and file_path:
            self._open_file(max_bytes, backups)

    # ------------------------------------------------------------ 文件
    def _open_file(self, max_bytes: int, backups: int) -> None:
        try:
            self._file = _RotatingFile(self.file_path, max_bytes, backups)
        except Exception as exc:
            self.file_error = f"{type(exc).__name__}: {exc}"
            self._file = None

    def close(self) -> None:
        f, self._file = self._file, None
        if f is not None:
            f.close()

    # ------------------------------------------------------------ 记录
    def _emit(self, level: str, msg: str, exc_text: str | None = None, *, to_host: bool = True) -> None:
        body = msg[len(PREFIX):] if msg.startswith(PREFIX) else msg
        ts = self.clock()
        line = f"{_iso(ts)} {level.upper():<7} {body}"
        if exc_text:
            line += "\n" + exc_text.rstrip()
        if self.enabled:
            with self._lock:
                self.ring.append(line)
                if level in ("error", "exception"):
                    self.error_count += 1
                    self.errors.append({"at": _iso(ts), "message": _clip(body, 500),
                                        "traceback": _clip(exc_text or "", 4000)})
            f = self._file
            if f is not None:
                try:
                    f.write_line(line)
                    self.file_error = None
                except Exception as exc:     # 写盘失败不能影响业务，记下原因给诊断包看
                    self.file_error = f"{type(exc).__name__}: {exc}"
        if to_host and self.host is not None:
            try:
                fn = getattr(self.host, "error" if level == "exception" else level, None) or self.host.info
                fn(PREFIX + body + (("\n" + exc_text.rstrip()) if exc_text else ""))
            except Exception:
                pass

    def debug(self, msg: str, *a, **k) -> None:
        self._emit("debug", msg, to_host=False)

    def info(self, msg: str, *a, **k) -> None:
        self._emit("info", msg)

    def warning(self, msg: str, *a, **k) -> None:
        self._emit("warning", msg)

    def error(self, msg: str, *a, **k) -> None:
        self._emit("error", msg)

    def exception(self, msg: str, *a, **k) -> None:
        self._emit("exception", msg, traceback.format_exc())

    def run_note(self, msg: str) -> None:
        """每轮运行的开始 / 结束：只进日志文件和内存环，不刷宿主控制台。"""
        if self.log_runs:
            self._emit("info", msg, to_host=False)

    def note(self, key: str, value: Any) -> None:
        with self._lock:
            self.notes[key] = value

    # ------------------------------------------------------------ 钩子统计
    def guard(self, name: str, umo: str = "") -> _Guard:
        return _Guard(self, name, umo)

    def _hook_done(self, name: str, umo: str, ms: float, et, ev, tb) -> None:
        ok = et is None
        now = self.clock()
        exc_text = None
        if not ok:
            exc_text = "".join(traceback.format_exception(et, ev, tb))
        with self._lock:
            h = self.hooks.setdefault(name, {"count": 0, "errors": 0, "total_ms": 0.0,
                                             "max_ms": 0.0, "last_at": None, "last_error": None,
                                             "last_error_ts": None})
            h["count"] += 1
            h["total_ms"] += ms
            h["max_ms"] = max(h["max_ms"], ms)
            h["last_at"] = now
            if not ok:
                h["errors"] += 1
                h["last_error"] = _clip(f"{et.__name__}: {ev}", 300)
                h["last_error_ts"] = now
            self.trace.append((now, name, mask_umo(umo), round(ms, 2), ok))
        if not ok and issubclass(et, Exception):
            self._emit("error", f"{name} 钩子出错：{et.__name__}: {_clip(ev, 200)}", exc_text)
        if ms >= SLOW_HOOK_MS:
            last = self._slow_logged.get(name, 0.0)
            if now - last >= 30:
                self._slow_logged[name] = now
                self._emit("warning", f"{name} 钩子耗时 {ms:.0f} ms（钩子在事件循环里同步跑，太慢会拖住别的消息）")

    # ------------------------------------------------------------ 读取
    def recent(self, n: int = 300) -> list[str]:
        with self._lock:
            return list(self.ring)[-max(1, n):]

    def tail_file(self, max_lines: int = 1000) -> list[str]:
        """日志文件（含滚动出去的旧文件）的最后若干行，旧的在前；读不到就返回空。"""
        if not self.file_path:
            return []
        lines: list[str] = []
        for suffix in (".3", ".2", ".1", ""):  # 旧 → 新
            p = Path(self.file_path + suffix)
            try:
                if p.exists():
                    lines += p.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
        return lines[-max_lines:]

    def hook_stats(self) -> dict:
        with self._lock:
            out = {}
            for k, h in self.hooks.items():
                out[k] = {"count": h["count"], "errors": h["errors"],
                          "avg_ms": round(h["total_ms"] / h["count"], 2) if h["count"] else 0,
                          "max_ms": round(h["max_ms"], 2), "last_at": _iso(h["last_at"]),
                          "last_error": h["last_error"], "last_error_ts": h.get("last_error_ts")}
            return out

    def trace_list(self, n: int = 300) -> list[dict]:
        with self._lock:
            items = list(self.trace)[-n:]
        return [{"at": _iso(t), "hook": name, "umo": u, "ms": ms, "ok": ok} for t, name, u, ms, ok in items]


# =================================================================== 库快照

def _db_snapshot(store: Any, recent_n: int) -> dict:
    conn = store._read_conn()
    out: dict[str, Any] = {"sqlite_version": sqlite3.sqlite_version, "fts": getattr(store, "fts", None)}
    try:
        p = Path(store.path)
        out["db_bytes"] = p.stat().st_size if p.exists() else None
        wal = Path(str(p) + "-wal")
        out["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
    except Exception:
        pass
    try:
        out["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
    except Exception:
        pass
    out["indexes"] = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    r = conn.execute("SELECT COUNT(*) c, MIN(COALESCE(sort_at,0)) lo, MAX(COALESCE(sort_at,0)) hi FROM runs").fetchone()
    out["runs"] = r["c"]
    out["oldest_run_at"] = _iso(r["lo"]) if r["c"] else None
    out["newest_run_at"] = _iso(r["hi"]) if r["c"] else None
    out["events"] = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    out["blobs"] = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    out["umos"] = conn.execute("SELECT COUNT(*) FROM umos").fetchone()[0]
    out["runs_by_source_status"] = [
        {"source": r["source"], "status": r["status"], "n": r["n"]} for r in conn.execute(
            "SELECT source, status, COUNT(*) n FROM runs GROUP BY source, status ORDER BY n DESC")]
    live = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN stats_id IS NOT NULL THEN 1 ELSE 0 END) linked, "
        "SUM(CASE WHEN tools_available IS NULL OR tools_available IN ('[]','') THEN 1 ELSE 0 END) no_tools "
        "FROM runs WHERE source='live'").fetchone()
    out["live_runs"] = {"n": live["n"], "linked_to_provider_stats": live["linked"] or 0,
                        "without_tools_available": live["no_tools"] or 0}
    return out


def _recent_runs(store: Any, n: int, raw: bool, umo_map: dict) -> list[dict]:
    conn = store._read_conn()
    rows = conn.execute(
        "SELECT run_id, umo, source, status, sort_at, begin_at, end_at, n_steps, n_tools, n_errors, "
        "token_in, token_cached, token_out, model, stats_id, tools_available, substr(error_text,1,200) AS err "
        "FROM runs WHERE source IN ('live','note') ORDER BY COALESCE(sort_at,0) DESC, ord DESC, rowid DESC LIMIT ?",
        (n,)).fetchall()
    out = []
    for r in rows:
        try:
            n_tools_av = len(json.loads(r["tools_available"])) if r["tools_available"] else 0
        except Exception:
            n_tools_av = None
        m = mask_umo(r["umo"])
        if raw:
            umo_map[m] = r["umo"]
        dur = None
        if r["begin_at"] is not None and r["end_at"] is not None:
            dur = round(r["end_at"] - r["begin_at"], 2)
        out.append({
            "run_id": r["run_id"], "umo": m, "source": r["source"], "status": r["status"],
            "at": _iso(r["sort_at"]), "duration_s": dur, "steps": r["n_steps"], "tools": r["n_tools"],
            "errors": r["n_errors"], "tokens_in": r["token_in"], "tokens_cached": r["token_cached"],
            "tokens_out": r["token_out"],
            "model": r["model"], "stats_linked": r["stats_id"] is not None,
            "tools_available": n_tools_av, "error_text": r["err"] or None,
        })
    return out


# =================================================================== 导出

def build_bundle(store: Any, recorder: Any, diag: DiagLog, *, config: dict | None = None,
                 runtime: dict | None = None, importer: Any = None, raw: bool = False,
                 log_lines: int = 600, recent_n: int = 40) -> dict:
    now = diag.clock()
    umo_map: dict[str, str] = {}
    cfg_out = {}
    for k, v in (config or {}).items():
        if _SECRET_KEY.search(str(k)):
            cfg_out[k] = "<已隐藏>"
        elif k == "exclude_umos" and isinstance(v, (list, tuple)) and not raw:
            cfg_out[k] = [mask_umo(x) for x in v]
        else:
            cfg_out[k] = v
    bundle: dict[str, Any] = {
        "format": FORMAT,
        "generated_at": _iso(now),
        "uptime_s": round(now - diag.started_at, 1),
        "masked_session_ids": not raw,
        "privacy": "不含聊天正文；会话ID已打码（勾选「保留原始会话ID」才附对照表）" if not raw
                   else "不含聊天正文；已附会话ID对照表（umo_map），发给别人前请留意",
        "env": {
            "python": sys.version.split()[0], "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version, **(diag.notes.get("env") or {}),
        },
        "config": cfg_out,
        "log_file": {"path": _redact_home(diag.file_path or ""), "error": diag.file_error,
                     "rotate_error": getattr(diag._file, "rotate_error", None),
                     "enabled": diag.enabled},
    }
    try:
        bundle["db"] = _db_snapshot(store, recent_n)
    except Exception as exc:
        bundle["db"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        bundle["recent_runs"] = _recent_runs(store, recent_n, raw, umo_map)
    except Exception as exc:
        bundle["recent_runs"] = {"error": f"{type(exc).__name__}: {exc}"}

    rt: dict[str, Any] = dict(runtime or {})
    if recorder is not None:
        try:
            with recorder._lock:
                rt["recorder"] = {
                    "open_runs": len(recorder.open_by_eid), "open_by_umo": len(recorder.open_by_umo),
                    "span_seq": len(recorder._span_seq), "received_at": len(recorder.received_at),
                    "prep_at": len(recorder.prep_at),
                }
            active = []
            for a in recorder.active_runs():
                m = mask_umo(a.get("umo"))
                if raw:
                    umo_map[m] = a.get("umo")
                active.append({"run_id": a["run_id"], "umo": m, "idle_s": a.get("idle_sec"),
                               "current_tool": a.get("current_tool"), "n_tools": a.get("n_tools")})
            rt["active_runs"] = active
        except Exception as exc:
            rt["recorder_error"] = f"{type(exc).__name__}: {exc}"
    bundle["runtime"] = rt
    if importer is not None:
        try:
            bundle["legacy_import"] = importer.status()
        except Exception as exc:
            bundle["legacy_import"] = {"error": f"{type(exc).__name__}: {exc}"}

    bundle["hooks"] = diag.hook_stats()
    bundle["hook_trace"] = diag.trace_list(300)
    bundle["notes"] = {k: v for k, v in diag.notes.items() if k != "env"}
    bundle["error_count"] = diag.error_count
    bundle["recent_errors"] = list(diag.errors)[-30:]
    lines = diag.tail_file(log_lines) or diag.recent(log_lines)
    bundle["log_tail"] = [_redact_home(x) for x in lines]
    if raw:
        bundle["umo_map"] = umo_map
    return bundle
