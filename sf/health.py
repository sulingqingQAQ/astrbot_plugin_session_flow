# -*- coding: utf-8 -*-
"""页面顶部横幅用的自检（v0.1.8）：只在真出问题时才有内容。

不算故障的：注入归属 available 为 null（= 还没有请求经过，包装要等第一次请求才做）、没有任何钩子触发。
算故障的：
- 注入归属 available 为 false（宿主内部接口对不上，注入归属 / 改动 / 处理函数耗时会退化，但记录还在）
- 后台任务已经停了（写库任务停了 = 之后什么都存不进去）
- 最近半小时内钩子报过错
- 写库队列堆得太多、单个写库任务卡了超过一分钟，或最近半小时写库失败过
"""

from __future__ import annotations

from typing import Any

RECENT_S = 1800          # “近期”= 最近半小时
QUEUE_LIMIT = 200        # 写库队列超过这个数算堆积
STUCK_S = 60             # 单个写库任务跑超过这么久算卡住

TASK_TXT = {"sf-writer": "写库", "sf-watchdog": "看门狗（关掉卡死的运行）", "sf-align": "对齐服务商 token 统计",
            "sf-retention": "按保留天数清理旧记录"}


def _ago(now: float, ts: float) -> str:
    s = max(0, int(now - ts))
    return f"{s} 秒前" if s < 90 else f"{s // 60} 分钟前"


def evaluate(runtime: dict | None, hooks: dict | None, now: float) -> list[dict]:
    """→ [{code, level, title, text}]，空列表 = 一切正常。level: error 红 / warn 黄。"""
    out: list[dict] = []
    rt = runtime or {}

    st = ((rt.get("attribution") or {}).get("status")) or {}
    if st.get("available") is False:
        out.append({"code": "attrib_unavailable", "level": "warn", "title": "注入归属不可用",
                    "text": "AstrBot 的内部接口对不上（常见于 AstrBot 刚升级），所以没法看出每段注入是哪个插件加的、"
                            "谁改了历史和回复。聊天记录本身还在正常记。"
                            + (f"原因：{st.get('error')}" if st.get("error") else "")})

    for t in rt.get("tasks") or []:
        if t.get("done") and not t.get("cancelled"):
            name = TASK_TXT.get(t.get("name"), t.get("name"))
            out.append({"code": "task_dead:" + str(t.get("name")), "level": "error",
                        "title": f"后台任务「{name}」已经停了",
                        "text": ("写库任务停了，之后的记录存不进去。" if t.get("name") == "sf-writer" else "")
                                + "到「管理」页下载诊断包看原因，重载一下插件通常能恢复。"
                                + (f"出错信息：{t.get('error')}" if t.get("error") else "")})

    recent = []
    for name, h in (hooks or {}).items():
        ts = h.get("last_error_ts")
        if h.get("errors") and ts and now - ts <= RECENT_S:
            recent.append((ts, name, h))
    if recent:
        recent.sort(reverse=True)
        names = "、".join(n for _, n, _ in recent[:5])
        ts, name, h = recent[0]
        out.append({"code": "hook_errors", "level": "error", "title": f"最近半小时有 {len(recent)} 个钩子报过错",
                    "text": f"{names}。最近一次在{_ago(now, ts)}：{h.get('last_error') or '（没有详情）'}。"
                            "会话流的钩子出错不会影响机器人回复，但那一轮可能没记全。"})

    w = rt.get("write") or {}
    queue = rt.get("write_queue")
    busy = w.get("busy_since")
    if (isinstance(queue, int) and queue > QUEUE_LIMIT) or (busy and now - busy > STUCK_S):
        why = []
        if isinstance(queue, int) and queue > QUEUE_LIMIT:
            why.append(f"队列里攒了 {queue} 个待写任务")
        if busy and now - busy > STUCK_S:
            why.append(f"有一个写库任务已经跑了 {int(now - busy)} 秒")
        out.append({"code": "write_stuck", "level": "error", "title": "写库卡住了",
                    "text": "，".join(why) + "。库文件可能被别的程序占着，或磁盘太慢；记录会越攒越多，重载插件会把没写完的丢掉。"})
    if w.get("failed") and w.get("last_fail_at") and now - w["last_fail_at"] <= RECENT_S:
        out.append({"code": "write_failed", "level": "error", "title": "最近半小时写库失败过",
                    "text": f"累计失败 {w['failed']} 次，最近一次在{_ago(now, w['last_fail_at'])}："
                            f"{w.get('last_fail') or '（没有详情）'}。失败的那几轮没存进去。"})
    return out
