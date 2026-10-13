# -*- coding: utf-8 -*-
"""导出（v0.1.6 单轮，v0.1.8 起支持一整页 / 搜索结果和屏蔽）：把回合的全部内容导成一个文件，方便发给别人看。

和诊断包的区别：诊断包不含聊天正文；这个文件**含**回合的聊天正文、system prompt 全文、
每一项注入的全文和归属，所以默认只把会话 ID / 发送者 ID 打码，正文不做脱敏，发出去前自己看一眼。
要藏某一段（比如群聊最近消息）用“屏蔽”，见 sf/mask.py：被屏蔽的段整段删掉，文件头写一句“有 N 段被屏蔽”。

两种格式：
- md：给人和模型读，按时间顺序排，每个事件一小节，长文本放围栏代码块里不会被 Markdown 吃掉；
- json：不丢字段，机器好处理。

数据来源就是 webapi 的“单轮详情”（run + events + system prompt + 体积构成 + 时间拆分），
这里只做整理和渲染，不读库。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any

from . import diag as diag_mod
from . import mask as mask_mod

FORMAT = "session_flow_run/1"
FORMAT_MANY = "session_flow_runs/1"
MAX_BATCH_CHARS = 3_000_000      # 批量导出文件的字数上限（中文约 9 MB），超了后面的回合不放进来并写明

PRIVACY = ("含回合的聊天正文、system prompt 全文和每一项注入的全文。"
           "会话 ID / 发送者 ID 默认已打码，但正文没有脱敏（里面出现的昵称、号码、群名都是原样），发出去前请自己看一眼。")

# 这个文件里“没有”的东西，写明白，免得把没记的当成没发生
NOT_RECORDED = [
    "历史对话内容：只记了条数（contexts_len）、各角色的字数构成和“谁改了历史”的改动事件，历史消息本身没存。",
    "真正发给模型提供商的最终请求体：只存了会话流最高 / 最低优先级两个钩子拍的快照之差；最低优先级钩子之后再被改的内容看不到。",
    "单条超过「单段文本最大字数」的内容会被截断，截断处有“已截断，原长 N 字”标记。",
    "旧档（source=legacy）没有注入、改动、实际发出这些事件；v0.1.8 之前的记录没有体积构成和时间拆分。",
]

KIND_TXT = {"user": "用户", "text": "回复", "think": "思考", "tool_call": "调工具", "tool_result": "工具结果",
            "note": "备注", "error": "报错", "inject": "注入", "edit": "改动", "sent": "实际发出"}
SRC_TXT = {"system_prompt": "系统提示词", "prompt": "提问", "extra_part": "用户消息附加块"}
HOOK_TXT = {"on_llm_request": "请求前", "on_llm_response": "回复后", "on_decorating_result": "发送前"}

_ID_FIELDS = ("sender_id",)
_NAME_FIELDS = ("sender_name",)


def _tag(value: Any) -> str:
    s = str(value or "")
    return f"#{hashlib.sha1(s.encode('utf-8')).hexdigest()[:6]}" if s else ""


def _iso(ts: Any, ms: bool = False) -> str | None:
    if ts in (None, ""):
        return None
    try:
        f = float(ts)
        out = datetime.fromtimestamp(f).strftime("%Y-%m-%d %H:%M:%S")
        return out + f".{int((f % 1) * 1000):03d}" if ms else out
    except Exception:
        return None


def _dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _model_text(run: dict) -> str:
    pid, model = str(run.get("provider_id") or ""), str(run.get("model") or "")
    return pid if (not model or model in pid) else f"{pid} / {model}".strip(" /")


def inject_label(ev: dict) -> str:
    """和页面上“本轮注入 N 项，来自”的分组名一致。"""
    d = _dict(ev.get("data"))
    if d.get("attrib") == "exact":
        return str(d.get("plugin") or "?")
    if d.get("attrib") == "earlier":
        return "内核 / 更早"
    if d.get("attrib") == "unattributed":
        return "未归属"
    g = d.get("guess")
    return "疑似 " + str(g[0]) if g else "来源未知"


def attrib_text(ev: dict) -> str:
    d = _dict(ev.get("data"))
    a = d.get("attrib")
    if a == "exact":
        s = f"由 {d.get('plugin')} · {d.get('handler')}（优先级 {d.get('priority', '?')}）"
        return s + ("；后来又被别的处理函数改掉" if d.get("gone") else "")
    if a == "earlier":
        return "更早就在（AstrBot 内核，或优先级比会话流还高的插件）"
    if a == "unattributed":
        return "没抓到是谁"
    g = d.get("guess")
    return "疑似 " + " / ".join(map(str, g)) + "（只是推测）" if g else ""


# =================================================================== 组装
def build(payload: dict, *, raw: bool = False, version: str = "", now: float | None = None,
          masked_segments: int = 0) -> dict:
    """payload：{"run", "events"(已不含 ctx / timing), "sysprompt"(带 text) | None, "context", "timing"}。
    masked_segments：这一轮被屏蔽掉的段数（事件已经在 payload 里删掉了，这里只写进文件头）。"""
    run = dict(_dict(payload.get("run")))
    events = [dict(e) for e in (payload.get("events") or [])]
    sp = payload.get("sysprompt")
    if not raw:
        run["umo"] = diag_mod.mask_umo(run.get("umo"))
        for k in _ID_FIELDS + _NAME_FIELDS:
            if run.get(k):
                run[k] = _tag(run[k])
    # 基准是库里的“消息时间”（平台给的时间戳，没有就用收到时间），所以平台投递、流水线早期阶段的耗时也算在偏移里
    t0 = run.get("sort_at") or run.get("received_at")

    out_events = []
    for e in events:
        t = e.get("t")
        rel = round(float(t) - float(t0), 3) if (t and t0) else None
        item = {"seq": e.get("seq"), "t": _iso(t, True), "t_rel_s": rel, "kind": e.get("kind"),
                "step": e.get("step"), "name": e.get("name"), "call_id": e.get("call_id"),
                "text": e.get("text") or "", "data": e.get("data"),
                "is_error": bool(e.get("is_error")), "dur_ms": e.get("dur_ms")}
        out_events.append(item)

    inj_groups: dict[str, dict] = {}
    for e in out_events:
        if e["kind"] != "inject":
            continue
        g = inj_groups.setdefault(inject_label(e), {"label": inject_label(e), "n": 0, "chars": 0})
        g["n"] += 1
        g["chars"] += len(e["text"])
    chg = [e for e in out_events if e["kind"] in ("edit", "sent")]

    return {
        "format": FORMAT,
        "exported_at": _iso(now if now is not None else time.time()),
        "plugin_version": version,
        "masked": not raw,
        "masked_segments": int(masked_segments or 0),
        "privacy": PRIVACY,
        "run": run,
        "system_prompt": ({"hash": sp.get("hash"), "size": sp.get("size"), "same_as_prev": sp.get("same_as_prev"),
                           "text": sp.get("text")} if isinstance(sp, dict) else None),
        "system_prompt_masked": bool(payload.get("masked_system_prompt")),
        "context": payload.get("context"),
        "timing": payload.get("timing"),
        "summary": {
            "events": len(out_events),
            "injects": sorted(inj_groups.values(), key=lambda g: -g["chars"]),
            "inject_total_chars": sum(g["chars"] for g in inj_groups.values()),
            "changes": len(chg),
        },
        "events": out_events,
        "not_recorded": NOT_RECORDED,
    }


def filename(bundle: dict, fmt: str) -> str:
    run = _dict(bundle.get("run"))
    stamp = _iso(run.get("sort_at")) or _iso(time.time())
    stamp = re.sub(r"[-: ]", "", stamp or "")
    stamp = re.sub(r"^(\d{8})(\d{6}).*$", r"\1-\2", stamp)
    rid = re.sub(r"[^0-9a-zA-Z]", "", str(run.get("run_id") or ""))[:8] or "run"
    return f"session_flow_run_{stamp}_{rid}.{'json' if fmt == 'json' else 'md'}"


def filename_many(count: int, fmt: str, now: float | None = None) -> str:
    stamp = re.sub(r"[-: ]", "", _iso(now if now is not None else time.time()) or "")
    stamp = re.sub(r"^(\d{8})(\d{6}).*$", r"\1-\2", stamp)
    return f"session_flow_runs_{stamp}_{count}rounds.{'json' if fmt == 'json' else 'md'}"


# =================================================================== 渲染
def _fence(text: str) -> tuple[str, str]:
    """围栏比文本里最长的反引号串多一个，正文里有 ``` 也不会提前收尾。"""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    f = "`" * max(3, longest + 1)
    return f, f


def _block(text: str) -> str:
    a, b = _fence(text)
    return f"{a}text\n{text}\n{b}"


def _kv(rows: list[tuple[str, Any]]) -> str:
    return "\n".join(f"- {k}：{v}" for k, v in rows if v not in (None, "", []))


def to_json(bundle: dict) -> str:
    return json.dumps(bundle, ensure_ascii=False, indent=2, default=str)


def _pct(x: Any) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return ""


def _context_lines(ctx: Any, run: dict) -> list[str]:
    """“上下文体积构成”一节的正文行。"""
    if not ctx:
        return ["没记到（v0.1.8 之前的记录，或这一轮没走到请求钩子）。"]
    if ctx.get("error"):
        return [f"没数到：{ctx['error']}"]
    L: list[str] = []
    head = f"历史 {ctx.get('n_msgs')} 条 · 数到的字符共 {ctx.get('total_chars')} 个"
    if ctx.get("checkpoints"):
        head += f"（另有 {ctx['checkpoints']} 条 checkpoint 内部标记没算）"
    if ctx.get("tool_calls"):
        head += f" · 其中助手发起的工具调用 {ctx['tool_calls']} 次"
    if ctx.get("images"):
        head += f" · 历史里的图片 {ctx['images']} 张（token 没算）"
    L.append(head)
    if ctx.get("provider_total"):
        line = (f"服务商报的输入合计 {ctx['provider_total']} token（未命中缓存 {ctx.get('token_in')} + "
                f"命中缓存 {ctx.get('token_cached')}）")
        if ctx.get("cache_rate") is not None:
            line += f"，缓存命中率 {_pct(ctx['cache_rate'])}"
        L.append(line)
    elif ctx.get("cache_rate") is not None:
        L.append(f"缓存命中率 {_pct(ctx['cache_rate'])}")
    L.append("")
    L.append("| 部分 | 条数 | 中文等宽字符 | 其余字符 | 约占（估算） | 约 token（估算） |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for r in ctx.get("rows") or []:
        L.append(f"| {r['label']} | {r['n']} | {r['cjk']} | {r['other']} | "
                 f"{_pct(r['share']) if 'share' in r else '—'} | {r['tokens'] if 'tokens' in r else '—'} |")
    if ctx.get("top"):
        L.append("")
        L.append("历史里最长的几条：")
        for t in ctx["top"]:
            L.append(f"- 第 {t['i']} 条 · {t['role']} · {t['chars']} 字 · 「{t['head']}」")
    if ctx.get("note"):
        L.append("")
        L.append("说明：" + ctx["note"] + "。")
    return L


def _timing_lines(tm: Any) -> list[str]:
    if not tm:
        return ["没记到（v0.1.8 之前的记录）。"]
    L = []
    segs = tm.get("segments") or []
    total = tm.get("total_s") or 0
    if segs:
        L.append(f"合计 {total:g} s（消息时间 → 走到发送前最后一个钩子）：")
        L.append("")
        L.append("| 阶段 | 秒 | 占比 |")
        L.append("|---|---:|---:|")
        for s in segs:
            L.append(f"| {s['label']} | {s['s']:g} | {_pct(s['s'] / total) if total else '—'} |")
    else:
        L.append("缺时间点，分不出段。")
    calls = tm.get("calls") or []
    if calls:
        L.append("")
        L.append("别的插件的处理函数（会话流包装得到的，按总耗时排）：")
        L.append("")
        L.append("| 钩子 | 插件 · 处理函数 | 次数 | 合计 ms | 最慢 ms |")
        L.append("|---|---|---:|---:|---:|")
        for c in calls[:30]:
            L.append(f"| {c.get('hook_label')} | {c.get('plugin')} · {c.get('handler')} | {c.get('n')} | "
                     f"{c.get('total_ms'):g} | {c.get('max_ms'):g} |")
    for n in tm.get("notes") or []:
        L.append("")
        L.append("说明：" + n + "。")
    return L


def to_markdown(bundle: dict, *, level: int = 0, sp_ref: str | None = None) -> str:
    """level：标题整体往下降几级（批量导出里每一轮是一章）；sp_ref：system prompt 放在附录里时的编号，
    给了就不重复贴全文。"""
    def H(n: int) -> str:
        return "#" * (n + level) + " "

    run = _dict(bundle.get("run"))
    sp = bundle.get("system_prompt")
    evs = bundle.get("events") or []
    summ = _dict(bundle.get("summary"))
    L: list[str] = []
    title = (run.get("user_text") or "").strip()[:60].replace(chr(10), " ")
    if level == 0:   # 批量导出里每一轮前面已经有“第 N 轮”的标题，不再重复
        L.append(H(1) + "会话流 · 单轮导出" + (f"：{title}" if title else ""))
        L.append("")
        L.append(f"> {bundle.get('privacy')}")
        L.append(f"> 导出时间 {bundle.get('exported_at')} · 插件 {bundle.get('plugin_version') or '?'} · "
                 f"会话ID{'已打码' if bundle.get('masked') else '为原始值'}")
        if bundle.get("masked_segments"):
            L.append(f"> {mask_mod.note_line(bundle['masked_segments'])}")
        L.append("")
    L.append(H(2) + "这一轮")
    dur = None
    if run.get("begin_at") and run.get("end_at"):
        dur = round(float(run["end_at"]) - float(run["begin_at"]), 2)
    tools = run.get("tools_available")
    L.append(_kv([
        ("run_id", run.get("run_id")), ("会话", run.get("umo")), ("来源 / 状态", f"{run.get('source')} / {run.get('status')}"),
        ("消息时间", _iso(run.get("sort_at"))),
        ("发送者", " / ".join(str(x) for x in (run.get("sender_name"), run.get("sender_id")) if x)),
        ("模型", _model_text(run)),
        ("步数 / 工具 / 报错", f"{run.get('n_steps')} / {run.get('n_tools')} / {run.get('n_errors')}"),
        ("用时（agent 开始→结束）", f"{dur} s" if dur is not None else None),
        ("首 token", f"{round(float(run['ttft']), 2)} s" if run.get("ttft") else None),
        ("token（未命中缓存的输入 / 命中缓存的输入 / 输出）",
         f"{run.get('token_in')} / {run.get('token_cached')} / {run.get('token_out')}"
         if any(run.get(k) is not None for k in ("token_in", "token_cached", "token_out")) else None),
        ("历史条数", run.get("contexts_len")),
        ("可用工具", f"{len(tools)} 个：{'、'.join(map(str, tools))}" if isinstance(tools, list) and tools else None),
        ("报错", run.get("error_text")),
    ]))
    L.append("")
    L.append(H(2) + "上下文体积构成（估算）")
    L.extend(_context_lines(bundle.get("context"), run))
    L.append("")
    L.append(H(2) + "时间拆分")
    L.extend(_timing_lines(bundle.get("timing")))
    L.append("")
    L.append(H(2) + "本轮注入 / 改动一览")
    injs = summ.get("injects") or []
    if injs:
        L.append(f"注入 {sum(g['n'] for g in injs)} 项，共 {summ.get('inject_total_chars')} 字，按字数排：")
        for g in injs:
            L.append(f"- {g['label']}：{g['n']} 项 · {g['chars']} 字")
    else:
        L.append("没有记到注入。")
    if summ.get("changes"):
        L.append("")
        L.append(f"改动 / 实际发出 {summ['changes']} 项（详见下面时间线里的“改动”“实际发出”）。")
    if not bundle.get("system_prompt_masked"):
        L.append("")
        L.append(H(2) + "system prompt")
        if sp_ref and isinstance(sp, dict):
            L.append(f"见文件末尾的附录 {sp_ref}（hash {str(sp.get('hash'))[:12]}）"
                     + ("，和上一轮相同" if sp.get("same_as_prev") else ""))
        elif isinstance(sp, dict) and sp.get("text") is not None:
            L.append(f"hash {str(sp.get('hash'))[:12]} · {sp.get('size')} 字节"
                     + ("（和上一轮相同）" if sp.get("same_as_prev") else ""))
            L.append("")
            L.append(_block(sp["text"]))
        else:
            L.append("没存（配置里没开存 system prompt，或这一轮没记到）。")
    L.append("")
    L.append(H(2) + "时间线")
    if not evs:
        L.append("没有事件。")
    for e in evs:
        d = _dict(e.get("data"))
        kind = e.get("kind")
        title = KIND_TXT.get(kind, kind)
        if e.get("name") and e["name"] != title:
            title += f" · {e['name']}"
        if e.get("is_error"):
            title += " · 出错"
        L.append("")
        L.append(H(3) + f"#{e.get('seq')} {title}")
        meta = []
        if e.get("t_rel_s") is not None:
            meta.append(f"距消息时间 {e['t_rel_s']:+g}s")
        if e.get("step"):
            meta.append(f"第 {e['step']} 步")
        if e.get("dur_ms") is not None:
            meta.append(f"耗时 {e['dur_ms']}ms")
        if d.get("source"):
            meta.append("来自" + SRC_TXT.get(d["source"], str(d["source"])))
        if kind in ("inject", "edit"):
            a = attrib_text(e)
            if a:
                meta.append(a)
        if kind == "edit" and d.get("plugin") and not d.get("attrib"):
            meta.append(f"{d.get('plugin')} · {d.get('handler')}")
        if e.get("call_id"):
            meta.append(f"call_id {e['call_id']}")
        if meta:
            L.append(" · ".join(meta))
        if e.get("text"):
            L.append("")
            L.append(_block(e["text"]))
        if d.get("removed"):
            L.append("")
            L.append("被换掉的原文：")
            L.append(_block(str(d["removed"])))
        if kind == "sent":
            rws = d.get("rewrites") or []
            if not rws:
                L.append("")
                L.append("发出内容和模型原话不一样，但没抓到是谁改的。")
            for w in rws:
                L.append("")
                L.append(f"- **{w.get('plugin')} · {w.get('handler')}**（{HOOK_TXT.get(w.get('hook'), w.get('hook'))}，"
                         f"优先级 {w.get('priority', '?')}）" + ("清空了消息" if w.get("cleared") else "改写："))
                if not w.get("cleared"):
                    L.append("")
                    L.append("改写前：")
                    L.append(_block(str(w.get("before") or "")))
                    L.append("改写后：")
                    L.append(_block(str(w.get("after") or "")))
        args = d.get("args") if d.get("args") is not None else d.get("arguments")
        if args is not None:
            L.append("")
            L.append("参数：")
            L.append(_block(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, indent=2)))
        if d.get("truncated"):
            L.append("")
            L.append(f"（原文 {d['truncated']} 字，已截断）")
    if level == 0:
        L.append("")
        L.append("## 这个文件里没有的东西")
        for s in bundle.get("not_recorded") or []:
            L.append(f"- {s}")
    L.append("")
    return "\n".join(L)


def render(bundle: dict, fmt: str) -> tuple[str, str]:
    """返回 (文件内容, mime)。"""
    if fmt == "json":
        return to_json(bundle), "application/json"
    return to_markdown(bundle), "text/markdown"


# =================================================================== 批量（一整页 / 搜索结果）
def render_many(bundles: list[dict], *, fmt: str, label: str, version: str = "", raw: bool = False,
                requested: int | None = None, now: float | None = None,
                cap: int | None = None) -> tuple[str, str, dict]:
    """把多轮合成一个文件。bundles 按时间从早到晚；相同的 system prompt（按 hash）只放一份在附录里。
    → (文件内容, mime, {included, skipped, chars, system_prompts})。超过 cap 字的后面几轮不放进来。"""
    requested = requested if requested is not None else len(bundles)
    cap = cap or MAX_BATCH_CHARS
    sps: dict[str, dict] = {}
    slim: list[dict] = []
    for b in bundles:
        sp = b.get("system_prompt")
        b2 = dict(b)
        if isinstance(sp, dict) and sp.get("hash") and sp.get("text") is not None:
            h = str(sp["hash"])
            reg = sps.setdefault(h, {"hash": h, "size": sp.get("size"), "text": sp["text"], "runs": 0})
            reg["runs"] += 1
            b2["system_prompt"] = {k: v for k, v in sp.items() if k != "text"} | {"ref": h}
        slim.append(b2)

    pieces: list[tuple[dict, str]] = []
    used = 0
    refs: dict[str, str] = {}
    for b in slim:
        sp = b.get("system_prompt") or {}
        ref = None
        if sp.get("ref"):
            ref = refs.setdefault(sp["ref"], f"S{len(refs) + 1}")
        piece = to_json(b) if fmt == "json" else to_markdown(b, level=1, sp_ref=ref)
        # 附录占的字数留够：每份约一份 system prompt 的字数（用到才算）
        extra = sum(len(sps[h]["text"]) for h, r in refs.items() if r and h in sps) if fmt != "json" else 0
        if used + len(piece) + extra > cap and pieces:
            break
        pieces.append((b, piece))
        used += len(piece)
    included = len(pieces)
    skipped = len(slim) - included
    used_refs = {(_dict(b.get("system_prompt")).get("ref")) for b, _ in pieces} - {None}
    appendix = {h: sps[h] for h in sps if h in used_refs}
    masked_segments = sum(int(b.get("masked_segments") or 0) for b, _ in pieces)   # 只算放进文件的这些轮
    meta = {"included": included, "skipped": skipped, "requested": requested,
            "system_prompts": len(appendix), "masked_segments": masked_segments}

    exported_at = _iso(now if now is not None else time.time())
    if fmt == "json":
        doc = {"format": FORMAT_MANY, "exported_at": exported_at, "plugin_version": version, "label": label,
               "masked": not raw, "masked_segments": int(masked_segments or 0), "privacy": PRIVACY,
               "requested": requested, "included": included, "skipped": skipped,
               "system_prompts": {h: {"size": v["size"], "runs": v["runs"], "text": v["text"]}
                                  for h, v in appendix.items()},
               "runs": [b for b, _ in pieces], "not_recorded": NOT_RECORDED}
        text = to_json(doc)
        meta["chars"] = len(text)
        return text, "application/json", meta

    L: list[str] = [f"# 会话流 · 批量导出：{label}", "", f"> {PRIVACY}",
                    f"> 导出时间 {exported_at} · 插件 {version or '?'} · 共 {included} 轮 · "
                    f"会话ID{'已打码' if not raw else '为原始值'} · 按时间从早到晚"]
    if masked_segments:
        L.append(f"> {mask_mod.note_line(masked_segments)}")
    if skipped:
        L.append(f"> 本次选了 {requested} 轮，文件超过 {cap} 字的上限，只放了前 {included} 轮，后面 {skipped} 轮没放进来"
                 "（缩小范围再导，或者换成 JSON 分批导）。")
    L += ["", "## 目录", ""]
    for i, (b, _) in enumerate(pieces, 1):
        r = _dict(b.get("run"))
        first = (r.get("user_text") or "").strip().replace("\n", " ")[:40]
        L.append(f"{i}. {_iso(r.get('sort_at')) or '?'} · {r.get('status')} · {first or '（提问已屏蔽或为空）'}"
                 f" · {str(r.get('run_id'))[:12]}")
    L.append("")
    for i, (b, piece) in enumerate(pieces, 1):
        q = (_dict(b.get("run")).get("user_text") or "").strip().replace("\n", " ")[:40]
        L.append(f"## 第 {i} 轮" + (f"：{q}" if q else ""))
        L.append("")
        L.append(piece)
    if appendix:
        L.append("## system prompt 附录")
        L.append("")
        L.append("同一份 system prompt 被多轮用到时只在这里放一份，各轮里写“见附录 Sx”。")
        for h, ref in refs.items():
            if h not in appendix:
                continue
            v = appendix[h]
            L.append("")
            L.append(f"### {ref} · hash {h[:12]} · {v['size']} 字节 · 被 {v['runs']} 轮用到")
            L.append("")
            L.append(_block(v["text"]))
    L += ["", "## 这个文件里没有的东西"]
    for s in NOT_RECORDED:
        L.append(f"- {s}")
    L.append("")
    text = "\n".join(L)
    meta["chars"] = len(text)
    return text, "text/markdown", meta
