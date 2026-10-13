# -*- coding: utf-8 -*-
"""构造 tests/data/legacy_fixture.db（合成数据，不含真实聊天内容）。

形状照 AstrBot 真实的旧档表（conversations / provider_stats / platform_message_history）：
- wc1：WebChat 会话，2 轮（第 2 轮只有提问）+ checkpoint + provider_stats 2 行(completed+error)
- qq1：QQ 群会话，1 轮，无 checkpoint，provider_stats 1 行 completed
- est1：1 轮，无 checkpoint 无 stats → estimated
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

WC_UMO = "webchat:FriendMessage:webchat!astrbot!fixture-uuid-0001"
QQ_UMO = "default:GroupMessage:100000999"
EST_UMO = "default:FriendMessage:99999"


def build(path: Path | None = None) -> Path:
    path = path or (DATA / "legacy_fixture.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    db = sqlite3.connect(str(path))
    db.execute("""CREATE TABLE conversations(
        conversation_id TEXT PRIMARY KEY, platform_id TEXT, user_id TEXT,
        content TEXT, created_at TEXT, updated_at TEXT)""")
    db.execute("""CREATE TABLE provider_stats(
        id INTEGER PRIMARY KEY, status TEXT, provider_id TEXT, provider_model TEXT,
        token_input_other INTEGER, token_input_cached INTEGER, token_output INTEGER,
        start_time REAL, end_time REAL, time_to_first_token REAL, conversation_id TEXT)""")
    db.execute("""CREATE TABLE platform_message_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT, platform_id TEXT, user_id TEXT,
        sender_id TEXT, sender_name TEXT, content TEXT,
        llm_checkpoint_id TEXT, created_at TEXT)""")

    # ---------------- wc1 ----------------
    wc_content = [
        {"role": "user", "content": (
            "掷个20面骰子<system_reminder>Current datetime: 2026-10-12</system_reminder>")},
        {"role": "assistant", "content": [{"type": "think", "think": "调工具"}],
         "tool_calls": [{"id": "call_w1", "type": "function",
                         "function": {"name": "sf_probe_dice", "arguments": '{"sides": 20}'}}]},
        {"role": "tool", "content": "sf_probe_dice 结果：8（20 面）", "tool_call_id": "call_w1"},
        {"role": "assistant", "content": [{"type": "text", "text": "掷出来是 8"}]},
        {"role": "_checkpoint", "content": {"id": "ck-fixture-1"}},
        {"role": "user", "content": "再掷一次 data:image/png;base64,iVBORw0KGgoAAAANSUhEUg 结束"},
        {"role": "_checkpoint", "content": {"id": "ck-fixture-2"}},
    ]
    db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?)",
               ("wc-cid-1", "webchat", WC_UMO, json.dumps(wc_content, ensure_ascii=False),
                "2026-10-12 15:33:20.000", "2026-10-12 15:34:00.000"))
    db.execute("INSERT INTO provider_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (251, "completed", "longcat", "deepseek-v4.1-flash",
                3424, 25216, 68, 1791761611.89, 1791761616.53, 1.42, "wc-cid-1"))
    db.execute("INSERT INTO provider_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (252, "error", "longcat", "deepseek-v4.1-flash",
                100, 200, 0, 1791761620.0, 1791761622.0, None, "wc-cid-1"))
    chain_u1 = json.dumps([{"type": "text", "text": "掷个20面骰子"}], ensure_ascii=False)
    chain_b1 = json.dumps([{"type": "text", "text": "掷出来是 8"}], ensure_ascii=False)
    db.execute("INSERT INTO platform_message_history(platform_id,user_id,sender_id,sender_name,content,llm_checkpoint_id,created_at)"
               " VALUES (?,?,?,?,?,?,?)",
               ("webchat", WC_UMO, "u1", "Web用户", chain_u1, "ck-fixture-1", "2026-10-12 23:33:29.609"))
    db.execute("INSERT INTO platform_message_history(platform_id,user_id,sender_id,sender_name,content,llm_checkpoint_id,created_at)"
               " VALUES (?,?,?,?,?,?,?)",
               ("webchat", WC_UMO, "bot", "", chain_b1, "ck-fixture-1", "2026-10-12 23:33:36.549"))
    chain_u2 = json.dumps([{"type": "text", "text": "再掷一次"}], ensure_ascii=False)
    chain_b2 = json.dumps([{"type": "text", "text": "All chat models failed: boom"}], ensure_ascii=False)
    db.execute("INSERT INTO platform_message_history(platform_id,user_id,sender_id,sender_name,content,llm_checkpoint_id,created_at)"
               " VALUES (?,?,?,?,?,?,?)",
               ("webchat", WC_UMO, "u1", "Web用户", chain_u2, "ck-fixture-2", "2026-10-12 23:34:01.000"))
    db.execute("INSERT INTO platform_message_history(platform_id,user_id,sender_id,sender_name,content,llm_checkpoint_id,created_at)"
               " VALUES (?,?,?,?,?,?,?)",
               ("webchat", WC_UMO, "bot", "", chain_b2, "ck-fixture-2", "2026-10-12 23:34:03.000"))

    # ---------------- qq1（无 checkpoint） ----------------
    qq_content = [
        {"role": "user", "content": "群里问一句"},
        {"role": "assistant", "content": [{"type": "text", "text": "群里回一句"}]},
    ]
    db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?)",
               ("qq-cid-1", "default", QQ_UMO, json.dumps(qq_content, ensure_ascii=False),
                "2026-10-11 10:00:00.000", "2026-10-11 10:01:00.000"))
    db.execute("INSERT INTO provider_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (301, "completed", "deepseek", "deepseek-flash",
                500, 1000, 50, 1791674400.0, 1791674405.0, 0.8, "qq-cid-1"))

    # ---------------- est1（无 checkpoint、无 stats） ----------------
    est_content = [
        {"role": "user", "content": "孤零零一句"},
        {"role": "assistant", "content": [{"type": "text", "text": "孤零零一回"}]},
    ]
    db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?)",
               ("est-cid-1", "default", EST_UMO, json.dumps(est_content, ensure_ascii=False),
                "2026-10-10 09:00:00.000", "2026-10-10 09:00:30.000"))
    db.commit()
    db.close()
    return path


if __name__ == "__main__":
    p = build()
    print("fixture:", p)
