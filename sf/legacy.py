# -*- coding: utf-8 -*-
"""旧档导入：把 AstrBot 对话表里的历史导成 legacy 运行。

数据源 data_v4.db 只读（mode=ro）。后台任务，可查进度，可重复执行。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import extract as ex


def parse_utc_ts(value: Any) -> float | None:
    """UTC 无时区字符串 → epoch 秒。解析不了返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        # "2026-10-12 23:33:29.609" / 带 T 的 ISO
        iso = s.replace(" ", "T", 1) if " " in s and "T" not in s else s
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


class LegacyImporter:
    def __init__(self, store, names, cfg: dict | None = None, *, log=None):
        self.store = store
        self.names = names
        self.max_text = int((cfg or {}).get("max_text_chars", 50000))
        self.max_tool_result = int((cfg or {}).get("max_tool_result_chars", 20000))
        self.log = log
        self._lock = threading.Lock()
        self._progress: dict = {
            "state": "idle", "total": 0, "done": 0,
            "imported_runs": 0, "skipped": 0, "errors": [], "finished_at": None,
        }
        self._running = False

    # ------------------------------------------------------------ 进度
    def status(self) -> dict:
        with self._lock:
            p = dict(self._progress)
            p["errors"] = list(p["errors"])
            return p

    def _set(self, **kw) -> None:
        with self._lock:
            self._progress.update(kw)

    def _add_error(self, msg: str) -> None:
        with self._lock:
            errs = self._progress["errors"]
            if len(errs) < 20:
                errs.append(msg)

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    # ------------------------------------------------------------ 入口
    def start(self, astrbot_db: str | None, *, force: bool = False) -> dict:
        """同步跑完（调用方用 asyncio.to_thread 包一层）。返回统计。"""
        with self._lock:
            if self._running:
                raise RuntimeError("旧档导入正在进行中")
            self._running = True
            self._progress.update({
                "state": "running", "total": 0, "done": 0,
                "imported_runs": 0, "skipped": 0, "errors": [], "finished_at": None,
            })
        try:
            return self._run(astrbot_db, force=force)
        finally:
            with self._lock:
                self._running = False

    # ------------------------------------------------------------ 主流程
    def _run(self, astrbot_db: str | None, *, force: bool) -> dict:
        try:
            has_db = bool(astrbot_db) and Path(astrbot_db).exists()
        except OSError:
            has_db = False  # 沙箱/权限导致 stat 失败：按找不到库处理
        if not has_db:
            self._set(state="error", finished_at=time.time())
            self._add_error("找不到 AstrBot 数据库")
            return self.status()
        src = sqlite3.connect(f"file:{Path(astrbot_db).as_posix()}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "conversations" not in tables:
                self._set(state="error", finished_at=time.time())
                self._add_error("astrbot 库里没有 conversations 表")
                return self.status()
            convs = list(src.execute(
                "SELECT conversation_id, platform_id, user_id, content, created_at, updated_at "
                "FROM conversations ORDER BY conversation_id").fetchall())
            self._set(total=len(convs))
            umos_touched: set[str] = set()
            for conv in convs:
                cid = conv["conversation_id"]
                try:
                    self._import_one(src, tables, conv, force=force, touched=umos_touched)
                except Exception as e:  # 单个对话失败不影响其他
                    self._add_error(f"{cid}: {e}")
                with self._lock:
                    self._progress["done"] += 1
            if umos_touched:
                self.store.recompute_umos(umos_touched)
            self._set(state="done", finished_at=time.time())
            return self.status()
        finally:
            try:
                src.close()
            except Exception:
                pass

    # ------------------------------------------------------------ 单个对话
    def _import_one(self, src: sqlite3.Connection, tables: set[str],
                    conv: sqlite3.Row, *, force: bool, touched: set[str]) -> None:
        cid = conv["conversation_id"]
        umo = conv["user_id"] or ""
        updated_at = conv["updated_at"]
        done_key = f"legacy_done:{cid}"
        if not force and self.store.get_meta(done_key) == str(updated_at):
            with self._lock:
                self._progress["skipped"] += 1
            return
        if force:
            self.store.delete_cid_runs(cid, source="legacy")

        try:
            content = json.loads(conv["content"]) if isinstance(conv["content"], str) else (conv["content"] or [])
        except (json.JSONDecodeError, TypeError):
            content = []
        if not isinstance(content, list):
            content = []

        rounds = self._split_rounds(content)
        if not rounds:
            self.store.set_meta(done_key, str(updated_at))
            return

        conv_created = parse_utc_ts(conv["created_at"])
        stats_rows = self._stats_for_cid(src, tables, cid)
        ck_map = self._checkpoints(src, tables, cid, conv["user_id"])

        # provider_stats 对应：先比全部行数，再比 completed
        stats_match: list | None = None
        if len(stats_rows) == len(rounds):
            stats_match = stats_rows
        else:
            completed = [r for r in stats_rows if r["status"] == "completed"]
            if len(completed) == len(rounds):
                stats_match = completed

        # 实时去重基线：同 cid 已有 live 运行
        live_runs = self.store.runs_for_cid(cid, source="live")

        for idx, rnd in enumerate(rounds):
            run_id = f"legacy:{cid}:{idx}"
            if self.store.get_run_row(run_id) is not None and not force:
                with self._lock:
                    self._progress["skipped"] += 1
                continue
            ck = ck_map.get(rnd.get("checkpoint"))
            ck_begin = parse_utc_ts(ck["user_at"]) if ck else None
            ck_end = parse_utc_ts(ck["bot_at"]) if ck else None
            stat = stats_match[idx] if stats_match is not None and idx < len(stats_match) else None

            if ck_begin is not None or ck_end is not None:
                begin_at, end_at = ck_begin, ck_end
                sort_at = ck_begin or ck_end or conv_created or time.time()
                time_source = "checkpoint"
            elif stat is not None:
                begin_at = stat["start_time"] or None
                end_at = stat["end_time"] or None
                sort_at = begin_at or conv_created or time.time()
                time_source = "provider_stats"
            else:
                begin_at, end_at = None, None
                sort_at = conv_created or time.time()
                time_source = "estimated"

            user_text, reply_text, events, n_steps, n_tools, n_errors, images = \
                self._round_to_events(rnd, sort_at)

            # 状态：默认 legacy；无回复或 stats error → error
            status = "legacy"
            error_text = None
            if not reply_text.strip():
                status = "error"
                if ck and ck.get("bot_text"):
                    error_text = (ck["bot_text"] or "")[:500] or "本轮没有助手回复"
                else:
                    error_text = "本轮没有助手回复"
            if stat is not None and stat["status"] == "error":
                status = "error"
                error_text = error_text or "模型调用失败（provider_stats）"

            if self._dup_of_live(live_runs, user_text, reply_text):
                with self._lock:
                    self._progress["skipped"] += 1
                continue

            sender_name = (ck.get("sender_name") if ck else None) or None
            display = self.names.resolve(umo)
            self.store.touch_umo(umo, conv["platform_id"], self._guess_mtype(umo), display)
            if stat is not None or (ck_begin is not None and stats_match is not None):
                # checkpoint + 行数对上时 token 照样从 stats 补
                srow = stat
            else:
                srow = stat
            self.store.insert_run({
                "run_id": run_id, "umo": umo, "cid": cid,
                "source": "legacy", "status": status,
                "sort_at": sort_at, "ord": idx, "time_source": time_source,
                "sender_id": None, "sender_name": sender_name,
                "begin_at": begin_at, "end_at": end_at,
                "provider_id": (srow["provider_id"] if srow else None),
                "model": (srow["provider_model"] if srow else None),
                "user_text": user_text[: self.max_text],
                "reply_text": reply_text[: self.max_text],
                "error_text": error_text,
                "n_steps": n_steps, "n_tools": n_tools, "n_errors": n_errors,
                "token_in": (srow["token_input_other"] if srow else None),
                "token_cached": (srow["token_input_cached"] if srow else None),
                "token_out": (srow["token_output"] if srow else None),
                "ttft": (srow["time_to_first_token"] or None if srow else None),
                "stats_id": (srow["id"] if srow else None),
            })
            self.store.insert_events([
                {"run_id": run_id, "seq": i + 1, **e} for i, e in enumerate(events)
            ])
            touched.add(umo)
            with self._lock:
                self._progress["imported_runs"] += 1
        self.store.set_meta(done_key, str(updated_at))

    # ------------------------------------------------------------ 切轮
    @staticmethod
    def _split_rounds(content: list) -> list[dict]:
        """→ [{user, assists:[], tools:[], checkpoint, notes:[]}]。"""
        rounds: list[dict] = []
        cur: dict | None = None
        pending_notes: list = []

        def new_round(user_msg: dict) -> dict:
            return {"user": user_msg, "assists": [], "tools": [],
                    "checkpoint": None, "notes": list(pending_notes)}

        for m in content:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "user":
                if cur is not None:
                    rounds.append(cur)
                    pending_notes = []
                cur = new_round(m)
            elif role == "_checkpoint":
                ck = m.get("content") or {}
                ck_id = ck.get("id") if isinstance(ck, dict) else None
                if cur is not None:
                    cur["checkpoint"] = ck_id
                # checkpoint 出现在 user 之前（失败轮 user 后无 assistant 也跟 checkpoint）：
                # 暂存给下一轮
                if cur is None and ck_id:
                    pending_notes.append({"_next_checkpoint": ck_id})
            elif role == "system":
                text = m.get("content") if isinstance(m.get("content"), str) else json.dumps(
                    m.get("content"), ensure_ascii=False, default=str)
                if cur is not None:
                    cur["notes"].append(text)
                else:
                    pending_notes.append(text)
            elif role == "assistant":
                if cur is None:
                    cur = new_round({"role": "user", "content": ""})
                cur["assists"].append(m)
            elif role == "tool":
                if cur is None:
                    cur = new_round({"role": "user", "content": ""})
                cur["tools"].append(m)
        if cur is not None:
            rounds.append(cur)
        # 把“user 之前”的 checkpoint 挪给后一轮
        carry = None
        for rnd in rounds:
            for n in list(rnd.get("notes", [])):
                if isinstance(n, dict) and "_next_checkpoint" in n:
                    carry = n["_next_checkpoint"]
                    rnd["notes"].remove(n)
            if carry and not rnd.get("checkpoint"):
                rnd["checkpoint"] = carry
                carry = None
        return rounds

    # ------------------------------------------------------------ checkpoint / stats
    @staticmethod
    def _checkpoints(src: sqlite3.Connection, tables: set[str], cid: str, user_id: str | None = None) -> dict:
        """checkpoint_id → {user_at, bot_at, bot_text, sender_name}。

        S10：按 user_id（会话）过滤，不全表掃。user_id 对不上时不过滤，
        免得某些库字段口径不同直接丢时间。
        """
        out: dict = {}
        if "platform_message_history" not in tables:
            return out
        sql = ("SELECT llm_checkpoint_id, sender_name, content, created_at "
               "FROM platform_message_history WHERE llm_checkpoint_id IS NOT NULL")
        args: list = []
        if user_id:
            try:
                has = src.execute(
                    "SELECT COUNT(*) FROM platform_message_history WHERE user_id=? LIMIT 1",
                    (user_id,)).fetchone()[0]
            except sqlite3.Error:
                has = 0
            if has:
                sql += " AND user_id=?"
                args.append(user_id)
        try:
            rows = src.execute(sql, args).fetchall()
        except sqlite3.Error:
            return out
        # cid 关联：bot/user 行里 content 链可能含 cid；最可靠的是顺序——这里先按 checkpoint 聚合，
        # 调用方只取本对话 rounds 引用的 checkpoint id。
        for r in rows:
            ck = r["llm_checkpoint_id"]
            if not ck:
                continue
            slot = out.setdefault(ck, {"user_at": None, "bot_at": None,
                                       "bot_text": "", "sender_name": None})
            text = LegacyImporter._chain_text(r["content"])
            # user 行短（纯提问）、bot 行长或报错：用是否有历史 user 文本区分太脆弱，
            # 按 created_at 先后：早的当开始，晚的当结束。
            t = r["created_at"]
            if slot["user_at"] is None or (t is not None and t < slot["user_at"]):
                slot["user_at"] = t
                if r["sender_name"]:
                    slot["sender_name"] = r["sender_name"]
            if slot["bot_at"] is None or (t is not None and t > slot["bot_at"]):
                slot["bot_at"] = t
                slot["bot_text"] = text
        return out

    @staticmethod
    def _chain_text(content: Any) -> str:
        try:
            chain = json.loads(content) if isinstance(content, str) else content
        except (json.JSONDecodeError, TypeError):
            return str(content or "")
        if isinstance(chain, list):
            parts = []
            for node in chain:
                if isinstance(node, dict):
                    for k in ("text", "content"):
                        if isinstance(node.get(k), str):
                            parts.append(node[k])
            return "\n".join(parts)
        if isinstance(chain, dict):
            return str(chain.get("text") or chain.get("content") or "")
        return str(chain or "")

    @staticmethod
    def _stats_for_cid(src: sqlite3.Connection, tables: set[str], cid: str) -> list:
        if "provider_stats" not in tables:
            return []
        try:
            return list(src.execute(
                "SELECT id, status, provider_id, provider_model, token_input_other, "
                "token_input_cached, token_output, start_time, end_time, time_to_first_token "
                "FROM provider_stats WHERE conversation_id=? ORDER BY start_time",
                (cid,)).fetchall())
        except sqlite3.Error:
            return []

    # ------------------------------------------------------------ 轮 → 事件
    def _round_to_events(self, rnd: dict, sort_at: float):
        events: list[dict] = []
        user_content = (rnd.get("user") or {}).get("content", "")
        spoken, injects, images = ex.split_user_content(user_content)
        spoken, n1 = ex.strip_images_in_text(spoken or "")
        images += n1
        user_text, cut = ex.truncate(spoken or "", self.max_text)
        udata: dict | None = dict(cut) if cut else None
        if images:
            udata = {**(udata or {}), "images": images}
        events.append({"t": sort_at, "kind": "user", "step": 0, "text": user_text, "data": udata})

        for inj in injects:
            itext, _ = ex.strip_images_in_text(inj["text"])
            itext, icut = ex.truncate(itext, self.max_text)
            events.append({"t": sort_at, "kind": "inject", "step": 0,
                           "name": inj["name"], "text": itext, "data": icut})

        n_steps = len(rnd["assists"])
        n_tools = 0
        n_errors = 0
        reply_text = ""
        step_no = 0
        pending_calls: list[dict] = []  # [{name,call_id}]
        for am in rnd["assists"]:
            step_no += 1
            for seg in ex.assistant_segments(am.get("content")):
                stext, _ = ex.strip_images_in_text(seg["text"])
                stext, scut = ex.truncate(stext, self.max_text)
                events.append({"t": None, "kind": seg["kind"], "step": step_no,
                               "text": stext, "data": scut})
                if seg["kind"] == "text":
                    reply_text = seg["text"]
            for tc in ex.tool_calls_of(am):
                n_tools += 1
                pending_calls.append(tc)
                events.append({"t": None, "kind": "tool_call", "step": step_no,
                               "name": tc["name"], "call_id": tc["id"] or None,
                               "text": ex.pretty_json_args(tc["arguments"])})
        for tm in rnd["tools"]:
            call_id = str(tm.get("tool_call_id") or "")
            name = ""
            for pc in pending_calls:
                if pc["id"] == call_id:
                    name = pc["name"]
                    break
            ctext = tm.get("content") if isinstance(tm.get("content"), str) else json.dumps(
                tm.get("content"), ensure_ascii=False, default=str)
            ctext, _ = ex.strip_images_in_text(ctext or "")
            is_err = ex.is_error_result_text(ctext or "")
            if is_err:
                n_errors += 1
            ctext, ccut = ex.truncate(ctext or "", self.max_tool_result)
            events.append({"t": None, "kind": "tool_result", "step": step_no,
                           "name": name or None, "call_id": call_id or None,
                           "text": ctext, "is_error": 1 if is_err else 0, "data": ccut})
        for note in rnd.get("notes", []):
            if isinstance(note, dict):
                continue
            ntext, ncut = ex.truncate(str(note), self.max_text)
            events.append({"t": None, "kind": "note", "step": 0, "text": ntext, "data": ncut})
        return user_text, reply_text, events, n_steps, n_tools, n_errors, images

    # ------------------------------------------------------------ 去重 / 杂项
    @staticmethod
    def _dup_of_live(live_runs: list, user_text: str, reply_text: str) -> bool:
        u = (user_text or "").strip()[:200]
        r = (reply_text or "").strip()[:200]
        if not u:
            return False
        for lr in live_runs:
            try:
                lu = (lr["user_text"] or "").strip()[:200]
                lrp = (lr["reply_text"] or "").strip()[:200]
            except (KeyError, TypeError):
                continue
            if lu == u and lrp == r:
                return True
        return False

    @staticmethod
    def _guess_mtype(umo: str) -> str:
        u = umo.upper()
        if "GROUP" in u:
            return "GroupMessage"
        if "FRIEND" in u:
            return "FriendMessage"
        return "OtherMessage"
