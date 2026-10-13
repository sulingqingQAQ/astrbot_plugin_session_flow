# -*- coding: utf-8 -*-
"""插件页后端接口。

路由注册成 /astrbot_plugin_session_flow/api/<名字>，
页面里用 AstrBotPluginPage.apiGet("api/<名字>") 调用。
返回 {"status":"ok","data":…}，出错 {"status":"error","message":…}。

业务逻辑写成 handle_xxx(args, body)，不依赖 quart，方便离线测试；
register() 里再包一层，从请求取参数。

S6：读库的 handle_xxx 都是同步函数，经 _threaded 丢到线程里跑，
不能在 AstrBot 事件循环里同步读库（库大时卡死整个循环）。
handle_legacy_import 例外：它只发后台任务，必须留在事件循环里。
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from typing import Any, Callable

from . import ctxsize as ctxsize_mod
from . import diag as diag_mod
from . import export as export_mod
from . import mask as mask_mod
from . import search as search_mod
from . import spdiff as spdiff_mod
from . import stats as stats_mod
from . import summary as summary_mod
from . import timing as timing_mod

PLUGIN_NAME = "astrbot_plugin_session_flow"
MAX_EXPORT_RUNS = 200      # 一次最多导多少轮（一页最大 200 条，搜索结果也只导前 200 轮）


def _threaded(fn):
    """同步 handle_xxx 包成 async：丢线程跑，事件循环不堵。"""
    @functools.wraps(fn)
    async def wrapper(self, args: dict, body: dict):
        return await asyncio.to_thread(fn, self, args, body)
    return wrapper


def ok(data: Any = None) -> dict:
    return {"status": "ok", "data": data if data is not None else {}}


def err(message: str) -> dict:
    return {"status": "error", "message": str(message)}


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _clip(text: Any, n: int = 300) -> str:
    t = text or ""
    if not isinstance(t, str):
        t = str(t)
    return t if len(t) <= n else t[:n] + "…"


class SessionFlowWebApi:
    def __init__(self, store, names, recorder=None, importer=None):
        self.store = store
        self.names = names
        self.recorder = recorder
        self.importer = importer
        # 诊断导出用，由 main.py 接上；离线测试里可以不接
        self.diag = None
        self.guesser = None
        self.config_probe = None
        self.runtime_probe = None
        self.plugin_version = ""    # 导出写在文件头里，由 main.py 接上
        self.health_probe = None    # 页面顶部横幅的自检，由 main.py 接上，返回问题列表

    # ------------------------------------------------------------ 日志
    def _log_exc(self, msg: str) -> None:
        """接口里的报错：记进诊断日志。诊断日志再转给宿主的 logger（main.py 传进来的 astrbot.api.logger），
        同时进诊断包的“最近报错”。没接诊断日志（离线测试）就不记。必须在 except 块里调用，才带得出堆栈。"""
        if self.diag is not None:
            self.diag.exception(msg)

    def _log_warn(self, msg: str) -> None:
        if self.diag is not None:
            self.diag.warning(msg)

    # ------------------------------------------------------------ 总览
    @_threaded
    def handle_overview(self, args: dict, body: dict) -> dict:
        type_ = str(args.get("type", "all"))
        if type_ not in ("all", "group", "private", "other"):
            type_ = "all"
        platform = args.get("platform") or None
        errors_only = _truthy(args.get("errors", ""))
        sort = str(args.get("sort", "recent"))
        if sort not in ("recent", "runs", "errors", "tokens"):
            sort = "recent"
        q = args.get("q") or None
        umos = self.store.list_umos(type_=type_, platform=platform,
                                    errors_only=errors_only, sort=sort, q=q)
        running = set()
        if self.recorder is not None:
            try:
                for a in self.recorder.active_runs():
                    running.add(a["umo"])
            except Exception:
                pass
        for u in umos:
            u["running"] = u["umo"] in running
        return ok({
            "stats": self.store.global_stats(),
            "platforms": self.store.platforms(),
            "tool_top": self.store.tool_top(10),
            "umos": umos,
        })

    # ------------------------------------------------------------ 回合列表
    def _decorate_runs(self, runs: list[dict]) -> None:
        """列表轻量化：正文截 300 字 + tools_used[]。"""
        conn = self.store._read_conn()
        for r in runs:
            r["user_text"] = _clip(r.get("user_text"))
            r["reply_text"] = _clip(r.get("reply_text"))
            try:
                rows = conn.execute(
                    "SELECT DISTINCT name FROM events WHERE run_id=? AND kind='tool_call'",
                    (r["run_id"],)).fetchall()
                r["tools_used"] = [x["name"] for x in rows if x["name"]]
            except Exception:
                r["tools_used"] = []

    def _umo_card(self, umo: str) -> dict:
        card = next((u for u in self.store.list_umos(q=None) if u["umo"] == umo), None)
        if card is None:
            card = {"umo": umo, "display_name": self.names.resolve(umo)}
        return card

    def _runs_paged(self, umo: str, args: dict) -> dict:
        """页码模式：新的在前。带 q / kind 就是“搜本对话”，没有就是整个会话的回合。"""
        size = _int(args.get("size", 20), 20, 1, 200)
        page = _int(args.get("page", 1), 1, 1, 10 ** 7)
        q = str(args.get("q") or "").strip()
        kind = search_mod.clean_kind(args.get("kind"))
        searching = bool(q or kind)
        if searching:
            sr = search_mod.search_runs_page(self.store, q, umo=umo, kind=kind, page=page, size=size)
            runs = self.store.runs_by_ids(sr["ids"])
            more = set(sr["more"])
            for r in runs:
                r["hits"] = sr["hits"].get(r["run_id"], [])
                r["hits_more"] = r["run_id"] in more
            total, pages, page = sr["total"], sr["pages"], sr["page"]
        else:
            total = self.store.count_runs(umo)
            pages = max(1, -(-total // size))
            page = min(page, pages)
            runs = self.store.list_runs_offset(umo, offset=(page - 1) * size, limit=size)
        self._decorate_runs(runs)
        return ok({"umo": self._umo_card(umo), "runs": runs, "total": total, "page": page,
                   "pages": pages, "size": size, "searching": searching})

    @_threaded
    def handle_runs(self, args: dict, body: dict) -> dict:
        umo = args.get("umo")
        if not umo:
            return err("缺少 umo 参数")
        if args.get("page") not in (None, ""):
            return self._runs_paged(umo, args)
        before = None
        if args.get("before"):
            try:
                parts = str(args["before"]).split(":")
                if len(parts) == 3:
                    before = (float(parts[0]), int(parts[1]), int(parts[2]))
                else:
                    s, o = str(args["before"]).split(":", 1)
                    before = (float(s), int(o))
            except (ValueError, TypeError):
                return err("before 格式错误，应为游标 next_before")
        limit = _int(args.get("limit", 50), 50, 1, 200)
        runs, has_more, next_before = self.store.list_runs_page(umo, before=before, limit=limit)
        self._decorate_runs(runs)
        return ok({"umo": self._umo_card(umo), "runs": runs, "has_more": has_more, "next_before": next_before})

    # ------------------------------------------------------------ 单轮详情
    def _run_payload(self, run_id: str) -> tuple[dict | None, str]:
        """单轮详情的数据：(payload, "") 或 (None, 错误信息)。handle_run 和导出共用。"""
        run = self.store.run_detail(run_id)
        if run is None:
            return None, "找不到这一轮"
        events = []
        meta_events = []   # 体积构成（ctx）和计时（timing）不是时间线上的事件，单独算成两块
        try:
            for e in self.store.events_for(run_id):
                d = dict(e)
                if d.get("data"):
                    try:
                        d["data"] = json.loads(d["data"])
                    except (ValueError, TypeError):
                        pass
                (meta_events if d.get("kind") in ("ctx", "timing") else events).append(d)
        except Exception as exc:
            return None, f"读事件失败：{exc}"
        if self.guesser is not None:
            for d in events:
                if d.get("kind") != "inject":
                    continue
                data = d.get("data") if isinstance(d.get("data"), dict) else {}
                if data.get("attrib") == "exact":
                    continue
                try:
                    cand = self.guesser.guess(d.get("name"))
                except Exception:
                    cand = []
                if cand:
                    d["data"] = {**data, "guess": cand}
        sysprompt = None
        h = run.get("sysprompt_hash")
        if h:
            blob = self.store.get_blob(h)
            prev = self.store.prev_run(run.get("umo"), run.get("sort_at"), run.get("ord") or 0)
            prev_hash = None
            try:
                prev_hash = prev["sysprompt_hash"] if prev else None
            except (KeyError, TypeError, IndexError):
                prev_hash = None
            sysprompt = {"hash": h, "size": blob["size"] if blob else None,
                         "same_as_prev": bool(prev_hash) and prev_hash == h}
        return {"run": run, "events": events, "sysprompt": sysprompt,
                "context": self._context_block(run, meta_events),
                "timing": self._timing_block(run, meta_events)}, ""

    @staticmethod
    def _context_block(run: dict, meta_events: list[dict]) -> dict | None:
        """体积构成：记录时量的字数 / 条数 + 现算的折算（需要服务商 token 总数，没有就只给字数）。"""
        ev = next((e for e in meta_events if e.get("kind") == "ctx"), None)
        if ev is None:
            return None
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if data.get("error"):
            return {"error": str(data["error"])}
        tin, tcached = run.get("token_in"), run.get("token_cached")
        total = (tin or 0) + (tcached or 0)
        est = ctxsize_mod.estimate(data, total if total > 0 else None) or {}
        est["token_in"], est["token_cached"] = tin, tcached
        est["cache_rate"] = round(tcached / total, 4) if (total > 0 and tcached is not None) else None
        return est

    @staticmethod
    def _timing_block(run: dict, meta_events: list[dict]) -> dict | None:
        try:
            return timing_mod.build(run, meta_events)
        except Exception:
            self._log_exc("算时间拆分出错")
            return None

    @_threaded
    def handle_run(self, args: dict, body: dict) -> dict:
        run_id = args.get("id")
        if not run_id:
            return err("缺少 id 参数")
        payload, msg = self._run_payload(run_id)
        if payload is None:
            return err(msg)
        return ok(payload)

    # ------------------------------------------------------------ 单轮导出
    def _mask_rules_for(self, args: dict) -> list[dict]:
        """这一次导出要用的屏蔽规则：库里开着的（mask=0 时不用）+ 这一次临时勾选的整种屏蔽（mask_kinds=逗号分隔）。"""
        stored = mask_mod.load_rules(self.store) if _truthy(args.get("mask", "1")) else []
        once = [k.strip() for k in str(args.get("mask_kinds") or "").split(",") if k.strip()]
        return mask_mod.active_rules(stored, once)

    def _export_bundle(self, run_id: str, *, raw: bool, rules: list[dict]) -> tuple[dict | None, str]:
        """取一轮 → 补上 system prompt 全文 → 屏蔽 → 组装。→ (bundle, "") 或 (None, 错误信息)。"""
        payload, msg = self._run_payload(run_id)
        if payload is None:
            return None, msg
        sp = payload.get("sysprompt")
        if sp:
            try:
                blob = self.store.get_blob(sp["hash"])
            except Exception:
                blob = None
            sp = {**sp, "text": blob["text"] if blob else None}
        payload, n_masked = mask_mod.apply({**payload, "sysprompt": sp}, rules)
        return export_mod.build(payload, raw=raw, version=self.plugin_version, masked_segments=n_masked), ""

    @_threaded
    def handle_export(self, args: dict, body: dict) -> dict:
        """把一轮的全部内容（含正文、system prompt 全文、注入全文和归属）整理成一个文件的内容。
        fmt=md（默认）/ json；raw=1 才保留原始会话 ID 和发送者，默认打码；
        mask=0 不用库里的屏蔽规则，mask_kinds=system_prompt,inject 这一次再整种屏蔽。"""
        run_id = args.get("id")
        if not run_id:
            return err("缺少 id 参数")
        fmt = "json" if str(args.get("fmt", "md")).strip().lower() == "json" else "md"
        raw = _truthy(args.get("raw", "0"))
        try:
            bundle, msg = self._export_bundle(run_id, raw=raw, rules=self._mask_rules_for(args))
            if bundle is None:
                return err(msg)
            text, mime = export_mod.render(bundle, fmt)
        except Exception as exc:
            self._log_exc("生成单轮导出失败")
            return err(f"生成导出失败：{exc}")
        summ = bundle.get("summary") or {}
        sp = bundle.get("system_prompt")
        return ok({"filename": export_mod.filename(bundle, fmt), "mime": mime, "fmt": fmt, "text": text,
                   "chars": len(text), "masked": not raw, "masked_segments": bundle.get("masked_segments", 0),
                   "events": summ.get("events"),
                   "injects": sum(g["n"] for g in summ.get("injects") or []),
                   "inject_chars": summ.get("inject_total_chars"),
                   "system_prompt_chars": len(sp["text"]) if sp and sp.get("text") else 0})

    @_threaded
    def handle_export_many(self, args: dict, body: dict) -> dict:
        """导出一整页（scope=page，参数同 api/runs 的页码模式）或搜索结果（scope=search，带 q / kind，最多 200 轮）。
        文件按时间从早到晚，相同的 system prompt 只放一份，超过大小上限的后面几轮不放进来并写明。"""
        umo = args.get("umo")
        if not umo:
            return err("缺少 umo 参数")
        fmt = "json" if str(args.get("fmt", "md")).strip().lower() == "json" else "md"
        raw = _truthy(args.get("raw", "0"))
        q = str(args.get("q") or "").strip()
        kind = search_mod.clean_kind(args.get("kind"))
        searching = bool(q or kind)
        if str(args.get("scope") or "page") == "search":
            if not searching:
                return err("没有搜索条件，没法导出搜索结果")
            sr = search_mod.search_runs_page(self.store, q, umo=umo, kind=kind, page=1, size=MAX_EXPORT_RUNS)
            ids, total = sr["ids"], sr["total"]
            what = f"搜索结果（命中 {total} 轮" + (f"，只导出前 {len(ids)} 轮" if total > len(ids) else "") + "）"
        else:
            size = _int(args.get("size", 20), 20, 1, MAX_EXPORT_RUNS)
            page = _int(args.get("page", 1), 1, 1, 10 ** 7)
            if searching:
                sr = search_mod.search_runs_page(self.store, q, umo=umo, kind=kind, page=page, size=size)
                ids, page = sr["ids"], sr["page"]
                what = f"搜索结果第 {page} 页"
            else:
                total = self.store.count_runs(umo)
                page = min(page, max(1, -(-total // size)))
                ids = [r["run_id"] for r in self.store.list_runs_offset(umo, offset=(page - 1) * size, limit=size)]
                what = f"第 {page} 页（每页 {size} 条）"
        if not ids:
            return err("这一页没有可导出的回合")
        ids = list(reversed(ids))   # 列表是新的在前，文件按时间从早到晚
        rules = self._mask_rules_for(args)
        bundles, failed = [], 0
        try:
            for rid in ids:
                b, _msg = self._export_bundle(rid, raw=raw, rules=rules)
                if b is None:
                    failed += 1
                else:
                    bundles.append(b)
            if not bundles:
                return err("取不到这些回合的内容")
            label = f"{umo if raw else diag_mod.mask_umo(umo)} · {what}"
            text, mime, meta = export_mod.render_many(
                bundles, fmt=fmt, label=label, version=self.plugin_version, raw=raw, requested=len(ids))
        except Exception as exc:
            self._log_exc("生成批量导出失败")
            return err(f"生成导出失败：{exc}")
        return ok({"filename": export_mod.filename_many(meta["included"], fmt), "mime": mime, "fmt": fmt,
                   "text": text, "chars": meta["chars"], "masked": not raw, "requested": len(ids),
                   "included": meta["included"], "skipped": meta["skipped"], "failed": failed,
                   "system_prompts": meta["system_prompts"], "scope": what,
                   "masked_segments": meta["masked_segments"]})

    # ------------------------------------------------------------ 屏蔽规则 / 注入统计 / 自检横幅
    @_threaded
    def handle_mask_rules(self, args: dict, body: dict) -> dict:
        return ok({"rules": mask_mod.load_rules(self.store),
                   "kinds": [{"kind": k, "text": mask_mod.KIND_TXT[k], "nameless": k in mask_mod.NAMELESS}
                             for k in mask_mod.KINDS],
                   "seen": mask_mod.seen_names(self.store)})

    @_threaded
    def handle_mask_rules_save(self, args: dict, body: dict) -> dict:
        if not isinstance(body.get("rules"), list):
            return err("rules 必须是列表")
        try:
            rules = mask_mod.save_rules(self.store, body["rules"])
        except Exception as exc:
            return err(f"保存屏蔽规则失败：{exc}")
        return ok({"rules": rules})

    @_threaded
    def handle_inject_stats(self, args: dict, body: dict) -> dict:
        days = _int(args.get("days", 7), 7, 0, 3650) or None   # 0 = 全部
        try:
            return ok(stats_mod.inject_stats(self.store, self.guesser, days=days, umo=args.get("umo") or None))
        except Exception as exc:
            self._log_exc("注入统计出错")
            return err(f"统计出错：{exc}")

    @_threaded
    def handle_health(self, args: dict, body: dict) -> dict:
        if not callable(self.health_probe):
            return ok({"problems": [], "checked_at": time.time()})
        try:
            problems = self.health_probe()
        except Exception as exc:
            self._log_exc("自检出错")
            problems = [{"code": "health_failed", "level": "warn", "title": "自检本身出错了", "text": str(exc)}]
        return ok({"problems": problems, "checked_at": time.time()})

    @_threaded
    def handle_sysprompt(self, args: dict, body: dict) -> dict:
        h = args.get("hash")
        if not h:
            return err("缺少 hash 参数")
        blob = self.store.get_blob(h)
        if blob is None:
            return err("找不到这份 system prompt")
        return ok({"hash": h, "size": blob["size"], "text": blob["text"]})

    def _prompt_injects(self, run_id: str) -> list[dict]:
        """某一轮记到的、来自 system prompt 的注入块（带谁加的）。给 system prompt 对比找来源用。"""
        out: list[dict] = []
        try:
            rows = self.store.events_for(run_id)
        except Exception:
            return out
        for e in rows:
            d = dict(e)
            if d.get("kind") != "inject":
                continue
            data = d.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (ValueError, TypeError):
                    data = {}
            if not isinstance(data, dict) or data.get("source") != "system_prompt":
                continue
            item = {"name": d.get("name") or "", "text": d.get("text") or "",
                    "plugin": data.get("plugin"), "handler": data.get("handler"),
                    "attrib": data.get("attrib") or "unattributed"}
            if item["attrib"] != "exact" and self.guesser is not None:
                try:
                    item["guess"] = self.guesser.guess(item["name"])
                except Exception:
                    pass
            out.append(item)
        return out

    @_threaded
    def handle_sysprompt_diff(self, args: dict, body: dict) -> dict:
        """这一轮的 system prompt 和上一轮比，哪几处改了、谁加的。lines=1 才带逐行结果（完整提示词视图用）。"""
        run_id = args.get("id")
        if not run_id:
            return err("缺少 id 参数")
        run = self.store.run_detail(run_id)
        if run is None:
            return err("找不到这一轮")
        h = run.get("sysprompt_hash")
        if not h:
            return ok({"state": "no_prompt",
                       "text": "这一轮没记 system prompt（旧记录，或设置里“存完整 system prompt”关着）"})
        cur = self.store.get_blob(h)
        if cur is None:
            return ok({"state": "no_prompt", "text": "这一轮的 system prompt 原文已经被清理掉了"})
        # 上一轮：同一个会话里时间上前一轮；它没存提示词就再往前找，最多找 10 轮
        first_prev = prev = self.store.prev_run(run.get("umo"), run.get("sort_at"), run.get("ord") or 0)
        skipped = 0
        prev_blob = None
        for _ in range(10):
            if prev is None:
                break
            try:
                ph = prev["sysprompt_hash"]
            except (KeyError, IndexError, TypeError):
                ph = None
            if ph:
                prev_blob = self.store.get_blob(ph)
                if prev_blob is not None:
                    break
            skipped += 1
            prev = self.store.prev_run(prev["umo"], prev["sort_at"], prev["ord"] or 0)
        else:
            prev = None
        if prev is None or prev_blob is None:
            text = ("这是这个会话记到的第一轮，没有上一轮可以比" if first_prev is None
                    else "前面的几轮都没存 system prompt（或原文已被清理），没法比")
            return ok({"state": "no_prev", "text": text, "new_size": cur["size"]})
        info = {"run_id": prev["run_id"], "sort_at": prev["sort_at"], "skipped": skipped}
        base = {"prev": info, "old_size": prev_blob["size"], "new_size": cur["size"]}
        if prev_blob["hash"] == h:
            return ok({"state": "same", **base})
        res = spdiff_mod.build(prev_blob["text"], cur["text"],
                               self._prompt_injects(run_id), self._prompt_injects(prev["run_id"]),
                               with_lines=_truthy(args.get("lines", "")))
        return ok({"state": "changed" if (res["n_hunks"] or res.get("too_big") or res.get("note")) else "same",
                   **base, **res})

    # ------------------------------------------------------------ 检索 / 运行中 / 摘要
    @_threaded
    def handle_search(self, args: dict, body: dict) -> dict:
        q = (args.get("q") or "").strip()
        if not q:
            return err("缺少 q 参数")
        try:
            res = search_mod.search(self.store, self.names, q,
                                    umo=args.get("umo") or None,
                                    limit=_int(args.get("limit", 50), 50, 1, 200))
        except Exception as exc:
            self._log_exc("检索出错")
            return err(f"检索出错：{exc}")
        return ok(res)

    @_threaded
    def handle_active(self, args: dict, body: dict) -> dict:
        if self.recorder is None:
            return ok({"runs": []})
        try:
            runs = self.recorder.active_runs()
        except Exception as exc:
            return err(f"读运行中状态失败：{exc}")
        return ok({"runs": runs})

    @_threaded
    def handle_umo_summary(self, args: dict, body: dict) -> dict:
        umo = args.get("umo")
        if not umo:
            return err("缺少 umo 参数")
        try:
            return ok(summary_mod.umo_summary(self.store, umo))
        except Exception as exc:
            return err(f"摘要失败：{exc}")

    # ------------------------------------------------------------ 旧档导入
    async def handle_legacy_import(self, args: dict, body: dict) -> dict:
        if self.importer is None or self.recorder is None:
            return err("导入器未初始化")
        if self.importer.is_running():
            return err("旧档导入正在进行中")
        force = bool(body.get("force", False))

        import asyncio

        plugin = self

        async def _bg():
            try:
                db = getattr(plugin, "astrbot_db", None) or getattr(
                    plugin.recorder, "astrbot_db", None)
                await asyncio.to_thread(
                    plugin.importer.start, db, force=force)
            except Exception:
                self._log_exc("旧档导入失败")

        asyncio.get_running_loop().create_task(_bg())
        return ok({"started": True})

    @_threaded
    def handle_legacy_status(self, args: dict, body: dict) -> dict:
        if self.importer is None:
            return err("导入器未初始化")
        return ok(self.importer.status())

    # ------------------------------------------------------------ 管理
    @_threaded
    def handle_admin_stats(self, args: dict, body: dict) -> dict:
        return ok(self.store.admin_stats())

    @_threaded
    def handle_admin_purge(self, args: dict, body: dict) -> dict:
        days = body.get("days", args.get("days", 0))
        try:
            days = int(days)
        except (TypeError, ValueError):
            return err("days 必须是整数")
        if days <= 0:
            return err("days 必须大于 0")
        try:
            n = self.store.purge(days)
        except Exception as exc:
            return err(f"清理失败：{exc}")
        return ok({"deleted_runs": n})

    # ------------------------------------------------------------ 诊断
    @_threaded
    def handle_diag(self, args: dict, body: dict) -> dict:
        """诊断包：环境 / 配置 / 库状态 / 钩子统计 / 最近报错 / 日志尾巴。不含聊天正文。
        raw=1 才保留原始会话 ID（附对照表），默认打码。"""
        if self.diag is None:
            return err("诊断日志没启用")
        try:
            cfg = self.config_probe() if callable(self.config_probe) else {}
            rt = self.runtime_probe() if callable(self.runtime_probe) else {}
            bundle = diag_mod.build_bundle(
                self.store, self.recorder, self.diag, config=cfg, runtime=rt, importer=self.importer,
                raw=_truthy(args.get("raw", "0")), log_lines=_int(args.get("lines"), 600, 50, 3000))
        except Exception as exc:
            self._log_exc("生成诊断包失败")
            return err(f"生成诊断包失败：{exc}")
        return ok(bundle)

    # ------------------------------------------------------------ 注册
    def routes(self) -> list[tuple[str, Callable, list[str]]]:
        return [
            ("api/overview", self.handle_overview, ["GET"]),
            ("api/runs", self.handle_runs, ["GET"]),
            ("api/run", self.handle_run, ["GET"]),
            ("api/export", self.handle_export, ["GET"]),
            ("api/export_many", self.handle_export_many, ["GET"]),
            ("api/mask_rules", self.handle_mask_rules, ["GET"]),
            ("api/mask_rules/save", self.handle_mask_rules_save, ["POST"]),
            ("api/stats/inject", self.handle_inject_stats, ["GET"]),
            ("api/health", self.handle_health, ["GET"]),
            ("api/sysprompt", self.handle_sysprompt, ["GET"]),
            ("api/sysprompt_diff", self.handle_sysprompt_diff, ["GET"]),
            ("api/search", self.handle_search, ["GET"]),
            ("api/active", self.handle_active, ["GET"]),
            ("api/umo/summary", self.handle_umo_summary, ["GET"]),
            ("api/legacy/import", self.handle_legacy_import, ["POST"]),
            ("api/legacy/status", self.handle_legacy_status, ["GET"]),
            ("api/admin/stats", self.handle_admin_stats, ["GET"]),
            ("api/admin/purge", self.handle_admin_purge, ["POST"]),
            ("api/diag", self.handle_diag, ["GET"]),
        ]

    def register(self, context: Any, plugin_name: str = PLUGIN_NAME) -> int:
        reg = getattr(context, "register_web_api", None)
        if not callable(reg):
            self._log_warn("当前 AstrBot 不支持 register_web_api，插件页不可用")
            return 0

        def wrap(handler):
            async def view(**_path):
                from quart import request  # AstrBot 在调用时绑定了兼容的请求上下文
                try:
                    args = request.args.to_dict() if hasattr(request.args, "to_dict") else dict(request.args)
                    body: dict = {}
                    if request.method == "POST":
                        payload = await request.get_json(silent=True)
                        body = payload if isinstance(payload, dict) else {}
                    return await handler(args, body)
                except Exception as exc:
                    self._log_exc("接口出错")
                    return err(f"出错了：{exc}")

            view.__name__ = f"sf_{handler.__name__}"
            return view

        n = 0
        for route, handler, methods in self.routes():
            reg(f"/{plugin_name}/{route}", wrap(handler), methods,
                f"会话流：{route}")
            n += 1
        return n
