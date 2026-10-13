# -*- coding: utf-8 -*-
"""system prompt 和上一轮对比：哪几处改了、是谁加的 / 改的。

只用标准库。输入是两份 system prompt 全文，加上两轮各自记到的“注入块”（来源信息），
输出是改动处列表（每处写明谁带来的）和可选的逐行结果，页面据此把改动的地方画红框。

做法：
1. 先按行对比（difflib，不开 autojunk，免得空行 / 重复行被当垃圾丢掉）；
2. 行数一样的替换块按行配对，配上的行再按“词”对一遍（数字连着算一个词，汉字单个算一个），
   这样“时间 07:23 → 07:24”只框住变了的那几个字，而不是整行；
3. 每一处改动，用改动行去注入块文本里找：找得到就是那个插件 · 处理函数带来的，
   找不到就是“更早就在”（AstrBot 内核、优先级更高的插件，或人格 / 配置变了）。
"""

from __future__ import annotations

import difflib
import re
from typing import Any

MAX_CHARS = 600_000        # 任何一边超过就不逐行比，只告诉用户大小
MAX_LINES = 20_000         # 行数超过也不比
MAX_TOKENS = 6000          # 一行里词太多就整行算改，免得二次比对太慢
MAX_HUNKS = 300            # 最多给多少处写“谁加的”，再多说明整份基本都换了
PAIR_LIMIT = 2500          # 行数不等的替换块，old × new 超过这个就不去配对
SNIP = 400                 # 改动处里展示的新 / 旧文本最多多少字
MIN_KEY = 3                # 短于这么多字的行（空行、分隔线）不拿去找来源
MAX_WHO = 4                # 一处改动最多列几个来源

_TOK = re.compile(r"\s+|\d+|[A-Za-z_]+|[^\sA-Za-z0-9_]")
_WORDY = re.compile(r"\w")      # 至少有一个字母 / 数字 / 汉字；纯 --- === 这样的分隔线不拿去找来源


def _norm(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


# ------------------------------------------------------------------ 行内对比
def _fold(segs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """把夹在两处改动之间、只有一两个字的“没变”并进去（如 23:51 → 24:03 里的冒号），框连成一块。"""
    out: list[list] = []
    for k, t in segs:
        if k == 0:
            out.append(["eq", t])
            continue
        grp = out[-1] if out and out[-1][0] == "chg" else None
        if grp is None:
            grp = ["chg", "", ""]
            out.append(grp)
        if k == 2:
            grp[1] += t
        else:
            grp[2] += t
    res: list[list] = []
    i = 0
    while i < len(out):
        item = out[i]
        if (item[0] == "eq" and res and res[-1][0] == "chg" and i + 1 < len(out)
                and out[i + 1][0] == "chg" and len(item[1]) <= 2):
            res[-1][1] += item[1] + out[i + 1][1]
            res[-1][2] += item[1] + out[i + 1][2]
            i += 2
            continue
        res.append(list(item))
        i += 1
    flat: list[tuple[int, str]] = []
    for item in res:
        if item[0] == "eq":
            flat.append((0, item[1]))
        else:
            if item[1]:
                flat.append((2, item[1]))
            if item[2]:
                flat.append((1, item[2]))
    return flat


def inline(old: str, new: str) -> list[tuple[int, str]]:
    """同一行改前 / 改后 → [(kind, text)]。kind：0 没变，1 新的（画红框），2 被删的（画红虚线、划掉）。
    顺序是新行的顺序，被删的片段夹在原来的位置。两边差得太多（相似度 < 0.4）就整行当替换。"""
    if old == new:
        return [(0, new)]
    a, b = _TOK.findall(old), _TOK.findall(new)
    if len(a) > MAX_TOKENS or len(b) > MAX_TOKENS:
        return [s for s in ((2, old), (1, new)) if s[1]]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = sm.get_opcodes()
    same = sum(i2 - i1 for tag, i1, i2, _j1, _j2 in ops if tag == "equal")
    if (len(a) + len(b)) and same * 2 < (len(a) + len(b)) * 0.4:
        return [s for s in ((2, old), (1, new)) if s[1]]
    segs: list[tuple[int, str]] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            segs.append((0, "".join(b[j1:j2])))
            continue
        if i2 > i1:
            segs.append((2, "".join(a[i1:i2])))
        if j2 > j1:
            segs.append((1, "".join(b[j1:j2])))
    return _fold(segs)


# ------------------------------------------------------------------ 行配对
LONG_LINE = 400            # 超过这么长的行不跑精确相似度（太慢），用字符构成像不像来判断


def _sim(a: str, b: str) -> float:
    """两行像不像（0~1），只用来决定要不要把它们配成“同一行改了”。"""
    if not a or not b:
        return 0.0
    s = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if s.real_quick_ratio() < 0.55 or s.quick_ratio() < 0.55:
        return 0.0
    if len(a) > LONG_LINE or len(b) > LONG_LINE:
        q = s.quick_ratio()
        return q if q >= 0.8 else 0.0      # 长行只认“字符构成几乎一样”的，真假交给后面的按词对比
    return s.ratio()


def _align(old: list[str], new: list[str]) -> list[tuple[int | None, int | None]]:
    """一个替换块里的旧行和新行怎么配：返回 [(旧下标 / None, 新下标 / None)]，按显示顺序。
    行数相等就按位置配；不等就按相似度顺序往下找（块太大就不配，全当删 + 增）。"""
    if len(old) == len(new):
        return [(i, i) for i in range(len(old))]
    if len(old) * len(new) > PAIR_LIMIT:
        return [(i, None) for i in range(len(old))] + [(None, j) for j in range(len(new))]
    out: list[tuple[int | None, int | None]] = []
    oi = 0
    for nj, nl in enumerate(new):
        best, bs = None, 0.0
        for k in range(oi, len(old)):
            r = _sim(old[k], nl)
            if r > bs:
                bs, best = r, k
        if best is not None and bs >= 0.55:
            out += [(k, None) for k in range(oi, best)]
            out.append((best, nj))
            oi = best + 1
        else:
            out.append((None, nj))
    out += [(k, None) for k in range(oi, len(old))]
    return out


# ------------------------------------------------------------------ 谁带来的
def attribute(lines: list[str], injects: list[dict]) -> list[dict] | None:
    """这几行是被哪些注入块带进来的。
    返回 None = 没有可对的内容（全是空行 / 分隔线）；[] = 有内容但哪个注入块都不含；否则是来源列表。"""
    keys = [x.strip() for x in lines if len(x.strip()) >= MIN_KEY and _WORDY.search(x)]
    if not keys:
        return None
    scored: list[tuple[int, int, dict]] = []
    for idx, inj in enumerate(injects or []):
        text = inj.get("text") or ""
        if not text:
            continue
        n = sum(1 for k in keys if k in text)
        if n:
            scored.append((n, idx, inj))
    if not scored:
        return []
    top = max(s[0] for s in scored)
    keep = [s for s in scored if s[0] * 10 >= top * 3]      # 命中数不到最多的 3 成，多半是撞上了通用句子
    keep.sort(key=lambda s: (-s[0], s[1]))
    out: list[dict] = []
    seen: set = set()
    for _n, _i, inj in keep:
        key = (inj.get("plugin"), inj.get("handler"), inj.get("attrib"), inj.get("name"))
        if key in seen:
            continue
        seen.add(key)
        item = {"attrib": inj.get("attrib") or "unattributed", "name": inj.get("name") or ""}
        if inj.get("plugin"):
            item["plugin"] = inj["plugin"]
        if inj.get("handler"):
            item["handler"] = inj["handler"]
        if inj.get("guess"):
            item["guess"] = list(inj["guess"])
        out.append(item)
    return out


def _who_for(new_lines: list[str], old_lines: list[str], cur: list[dict], prev: list[dict]) -> list[dict]:
    """一处改动的“谁”：新一侧优先；新一侧没有（纯删除）看旧一侧；替换且旧一侧是别的来源，补一条。"""
    who: list[dict] = []

    def put(items, side):
        for it in items:
            who.append({**it, "side": side})

    if new_lines:
        got = attribute(new_lines, cur)
        if got is None:
            return []
        put(got if got else [{"attrib": "earlier"}], "new")
        if old_lines:
            old_got = attribute(old_lines, prev) or []
            mine = {(w.get("plugin"), w.get("handler")) for w in who if w.get("plugin")}
            put([o for o in old_got if o.get("plugin") and (o.get("plugin"), o.get("handler")) not in mine], "old")
    else:
        got = attribute(old_lines, prev)
        if got is None:
            return []
        put(got if got else [{"attrib": "earlier"}], "old")
    if len(who) > MAX_WHO:
        extra = len(who) - MAX_WHO
        who = who[:MAX_WHO] + [{"attrib": "more", "n": extra, "side": "new"}]
    return who


def _sig(who: list[dict]) -> frozenset:
    """一处改动的来源“签名”：谁（不分新旧）。两处相邻改动签名一样、且非空，就并成一处。"""
    return frozenset((w.get("attrib"), w.get("plugin"), w.get("handler")) for w in who if w.get("attrib") != "more")


# ------------------------------------------------------------------ 总入口
def _clip(text: str, n: int = SNIP) -> str:
    return text if len(text) <= n else text[:n] + "…"


def build(old_text: str, new_text: str, cur_injects: list[dict] | None = None,
          prev_injects: list[dict] | None = None, *, with_lines: bool = False) -> dict:
    """对比 old_text（上一轮）和 new_text（这一轮）。

    cur_injects / prev_injects：两轮各自记到的、来自 system prompt 的注入块
    [{name, text, plugin, handler, attrib, guess}]。with_lines=True 才带逐行结果（给“完整提示词”视图用）。
    """
    cur_injects = cur_injects or []
    prev_injects = prev_injects or []
    raw_same = (old_text == new_text)
    old_text, new_text = _norm(old_text), _norm(new_text)
    res: dict[str, Any] = {"identical": raw_same, "n_hunks": 0, "added": 0, "removed": 0,
                           "hunks": [], "who_summary": [], "truncated": False, "note": ""}
    if raw_same:
        return res
    if len(old_text) > MAX_CHARS or len(new_text) > MAX_CHARS:
        res["note"] = f"太大了（{len(old_text)} / {len(new_text)} 字），没逐行对比"
        res["too_big"] = True
        return res
    a, b = old_text.split("\n"), new_text.split("\n")
    if len(a) > MAX_LINES or len(b) > MAX_LINES:
        res["note"] = f"行数太多（{len(a)} / {len(b)} 行），没逐行对比"
        res["too_big"] = True
        return res

    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    lines: list[dict] = []
    hunks: list[dict] = []
    totals = [0, 0]

    def emit(items: list[dict], o_ls: list[str], n_ls: list[str], pos: int, h_add: int, h_del: int,
             who: list[dict] | None = None) -> None:
        """记一处改动。pos：这一处在新文本里从第几行开始（0 起）；who 已经算好就直接用。"""
        hid = len(hunks) + 1
        totals[0] += h_add
        totals[1] += h_del
        hunk = {"id": hid, "n": pos + 1, "old_lines": len(o_ls), "new_lines": len(n_ls),
                "added": h_add, "removed": h_del,
                "old": _clip("\n".join(o_ls)), "new": _clip("\n".join(n_ls))}
        if hid <= MAX_HUNKS:
            hunk["who"] = who if who is not None else _who_for(n_ls, o_ls, cur_injects, prev_injects)
        else:
            hunk["who"] = []
            res["truncated"] = True
        hunks.append(hunk)
        if with_lines:
            for it in items:
                it["h"] = hid
            lines.extend(items)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            if with_lines:
                lines.append({"k": "same", "n": j1 + 1, "t": "\n".join(b[j1:j2])})
            continue
        old_blk, new_blk = a[i1:i2], b[j1:j2]
        if tag == "insert":
            emit([{"k": "add", "n": j1 + nj + 1, "t": t} for nj, t in enumerate(new_blk)],
                 [], new_blk, j1, sum(len(t) + 1 for t in new_blk), 0)
            continue
        if tag == "delete":
            emit([{"k": "del", "n": j1 + 1, "t": t} for t in old_blk],
                 old_blk, [], j1, 0, sum(len(t) + 1 for t in old_blk))
            continue
        # 替换块：先配对，再按“配上的 / 只删的 / 只加的”拆成各自一处，免得几个不相干的改动并成一处、说不清谁改的
        runs: list[tuple[str, list[tuple[int | None, int | None]]]] = []
        for e in _align(old_blk, new_blk):
            cls = "chg" if (e[0] is not None and e[1] is not None) else ("del" if e[0] is not None else "add")
            if runs and runs[-1][0] == cls:
                runs[-1][1].append(e)
            else:
                runs.append((cls, [e]))
        recs: list[dict] = []
        seen_new = 0                      # 这个块里已经放出去的新行数，用来给“只删的”定位
        for cls, entries in runs:
            rec: dict = {"items": [], "o": [], "n": [], "add": 0, "del": 0, "pos": j1 + seen_new}
            for oi, nj in entries:
                if cls == "chg":
                    segs = inline(old_blk[oi], new_blk[nj])
                    rec["add"] += sum(len(t) for k, t in segs if k == 1)
                    rec["del"] += sum(len(t) for k, t in segs if k == 2)
                    rec["items"].append({"k": "chg", "n": j1 + nj + 1, "segs": [[k, t] for k, t in segs]})
                    rec["o"].append(old_blk[oi])
                    rec["n"].append(new_blk[nj])
                elif cls == "del":
                    rec["del"] += len(old_blk[oi]) + 1
                    rec["items"].append({"k": "del", "n": rec["pos"] + 1, "t": old_blk[oi]})
                    rec["o"].append(old_blk[oi])
                else:
                    rec["add"] += len(new_blk[nj]) + 1
                    rec["items"].append({"k": "add", "n": j1 + nj + 1, "t": new_blk[nj]})
                    rec["n"].append(new_blk[nj])
            recs.append(rec)
            seen_new += len(rec["n"])
        # 相邻两处如果是同一个来源带来的（例如记忆块里改了一行又追加一行），并回一处
        merged: list[dict] = []
        for rec in recs:
            if len(hunks) + len(merged) >= MAX_HUNKS:
                rec["who"] = None         # 改动处太多了，不再逐个找来源（emit 会记成 truncated）
                merged.append(rec)
                continue
            rec["who"] = _who_for(rec["n"], rec["o"], cur_injects, prev_injects)
            prev = merged[-1] if merged else None
            if prev is not None and prev["who"] is not None and _sig(prev["who"]) and _sig(prev["who"]) == _sig(rec["who"]):
                for key in ("items", "o", "n"):
                    prev[key] += rec[key]
                prev["add"] += rec["add"]
                prev["del"] += rec["del"]
                prev["who"] = _who_for(prev["n"], prev["o"], cur_injects, prev_injects)
            else:
                merged.append(rec)
        for m in merged:
            emit(m["items"], m["o"], m["n"], m["pos"], m["add"], m["del"], who=m["who"])
    added, removed = totals
    res["hunks"] = hunks
    res["n_hunks"] = len(hunks)
    res["added"], res["removed"] = added, removed
    if not hunks:
        res["note"] = "只有换行符不同"
    # 各来源出现了几处
    summ: dict[tuple, dict] = {}
    for h in hunks:
        seen_h: set = set()
        for w in h.get("who") or []:
            key = (w.get("attrib"), w.get("plugin"), w.get("handler"), w.get("side"))
            if key in seen_h or w.get("attrib") == "more":
                continue
            seen_h.add(key)
            e = summ.setdefault(key, {"attrib": w.get("attrib"), "plugin": w.get("plugin"),
                                      "handler": w.get("handler"), "side": w.get("side"), "n": 0,
                                      "guess": w.get("guess") or []})
            e["n"] += 1
    res["who_summary"] = sorted(summ.values(), key=lambda e: -e["n"])
    if with_lines:
        res["lines"] = lines
    return res
