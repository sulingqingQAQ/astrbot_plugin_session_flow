# 会话流 开发文档

> 对应版本 **v1.0.1**（许可证 AGPL-3.0，见仓库根目录的 `LICENSE`）。这份文档面向想读代码、改代码、接手维护的人；怎么用、有哪些功能见 [`README.md`](../README.md)。
> 文档里说的“钩子”都是 AstrBot 的插件事件钩子（`@filter.on_llm_request` 这一类）。

## 目录

1. [它是什么、遵守什么原则](#1-它是什么遵守什么原则)
2. [目录结构](#2-目录结构)
3. [运行时架构](#3-运行时架构)
4. [钩子与记录器（Recorder）](#4-钩子与记录器recorder)
5. [注入归属（Attributor）](#5-注入归属attributor)
6. [存储（Store）](#6-存储store)
7. [事件种类与 data 约定](#7-事件种类与-data-约定)
8. [功能模块](#8-功能模块)
9. [接口（webapi）](#9-接口webapi)
10. [管理页（pages/session_flow）](#10-管理页pagessession_flow)
11. [配置项](#11-配置项)
12. [测试](#12-测试)
13. [怎么加东西（清单）](#13-怎么加东西清单)
14. [宿主兼容性与非公开依赖](#14-宿主兼容性与非公开依赖)
15. [已知限制与没做的](#15-已知限制与没做的)
16. [版本历史](#16-版本历史)

---

## 1. 它是什么、遵守什么原则

会话流把 AstrBot 的**每一次 LLM 运行**（一个“回合”）记成一份**只追加的存档**：用户原话、思考、工具调用和结果、回复、报错、别的插件往提示词里塞了什么、system prompt 全文、token 和耗时……
然后在 AstrBot 的插件页里按会话回看、检索、导出。

AstrBot 自己的对话表是覆盖式快照，没有带时间戳的只追加历史，所以这些只能自己记。

**原则（改代码前先读一遍，很多设计是这几条推出来的）：**

| 原则 | 具体怎么做 |
|---|---|
| **只记录，不干预** | 所有钩子只读请求 / 响应对象；包装别的插件的处理函数时，原函数的参数、返回值、异常原样透传。 |
| **绝不拖慢、绝不弄坏回复** | 钩子里只做内存操作；写库经队列串行、放到线程里；每个钩子外面套 `DiagLog.guard`，**异常绝不外抛**；记录失败只留一条日志。 |
| **绝不阻塞事件循环** | 读库的接口处理函数全是同步函数，经 `_threaded` 丢到线程里跑（库大了读一次要几十毫秒到几秒）。 |
| **对宿主只读** | 读 AstrBot 的 `data_v4.db` 一律 `mode=ro`；不改宿主数据。 |
| **伸手进宿主内部要有退路** | 用到的非公开接口集中在 `sf/attrib.py`（见[第 14 节](#14-宿主兼容性与非公开依赖)）；对不上就把状态记成“不可用”并安静退出，只丢“精确归属”这一层，记录本身不受影响。 |
| **隐私** | 诊断包不含任何聊天正文、会话 ID 打码；导出含正文，所以默认只打码会话 ID / 发送者 ID，要藏某一段用“屏蔽”（不做自动脱敏）。日志里不写会话 ID。 |
| **没有第三方依赖** | 后端只用标准库（宿主提供 `astrbot`、`quart`）；页面是原生 JS + CSS，没有构建步骤。 |

---

## 2. 目录结构

```
astrbot_plugin_session_flow/
├── main.py                 插件入口：生命周期、4 个后台任务、钩子 → Recorder 的转发
├── metadata.yaml           插件元数据（版本号在这里，诊断包 / 导出会读它）
├── _conf_schema.json       配置项（每项都要写大白话 hint）
├── logo.png                插件图标（AstrBot 约定的文件名，放插件根目录；256×256 PNG）
├── LICENSE                 AGPL-3.0
├── .gitignore
├── .astrbot-plugin/i18n/   插件显示名 / 页面标题的本地化
├── sf/                     后端逻辑（全是可离线测试的纯 Python）
│   ├── recorder.py         记录器：钩子 → 内存状态机 → 串行落库（最大，约 1300 行）
│   ├── attrib.py           注入归属：包装别的插件的处理函数拍前后快照；疑似归属（源码字符串匹配）
│   ├── extract.py          消息解析 / 注入差分 / 发出内容签名 / 历史差分，全是纯函数
│   ├── store.py            SQLite 存储层（WAL，一个写连接 + 每线程一个只读连接）
│   ├── webapi.py           插件页后端接口（handle_xxx + 路由表 + 注册）
│   ├── names.py            会话（umo）显示名解析
│   ├── legacy.py           旧档导入（AstrBot conversations → legacy 运行）
│   ├── search.py           全文检索：语法解析 → SQL
│   ├── summary.py          会话规则摘要（纯 SQL 统计）
│   ├── diag.py             诊断日志（DiagLog）与诊断包
│   ├── export.py           单轮 / 批量导出（Markdown / JSON）
│   ├── mask.py             导出时屏蔽
│   ├── ctxsize.py          上下文体积构成（量字数 + 折算 token）
│   ├── timing.py           每轮时间拆分
│   ├── health.py           页面顶部横幅的自检
│   ├── stats.py            按插件统计注入量
│   └── spdiff.py           system prompt 和上一轮对比
├── pages/session_flow/     管理页：index.html / app.js / app.css（无依赖）
├── tests/                  离线用例（只用标准库）+ 本地预览小服务器
└── docs/DEVELOPMENT.md     本文
```

依赖方向（上层可以 import 下层，反过来不行）：

```
main.py ─► webapi ─► export / mask / search / stats / spdiff / timing / ctxsize / summary / diag ─► store
   │                                                                                                  ▲
   └──────► recorder ─► extract / ctxsize ──────────────────────────────────────────────────────────┘
   └──────► attrib   ─► extract
```

`main.py` 是唯一在模块顶部 import `astrbot` 的文件；`main.py` 和 `attrib.py` 里还有几处**在函数内按需** import 宿主的内部模块（见第 14 节），取不到就退回，其余模块完全不依赖宿主，所以测试里不用装 AstrBot。

---

## 3. 运行时架构

### 3.1 数据流

```
 AstrBot 事件循环
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ 钩子（main.py，只转发）        别的插件的处理函数                         │
 │   │                              ▲                                      │
 │   ▼                              │ attrib 包了一层，拍前后快照、计时       │
 │ Recorder（内存状态机，持 RLock）                                          │
 │   │ _enqueue(job)                                                       │
 │   ▼                                                                     │
 │ asyncio.Queue ──► sf-writer 任务 ──► asyncio.to_thread(job, store)       │
 └───────────────────────────────────────┬─────────────────────────────────┘
                                         ▼ 串行写（一个写连接）
                                 SQLite（WAL）  session_flow.db
                                         ▲ 只读连接（每线程一个）
                         SessionFlowWebApi.handle_xxx（_threaded 丢线程）
                                         ▲ register_web_api 路由
                          插件页 app.js ── window.AstrBotPluginPage.apiGet / apiPost
```

### 3.2 线程与并发

- **事件循环线程**：钩子、`Recorder` 的状态机、接口的注册和 `legacy/import`（只发后台任务）。
- **写**：`Recorder._enqueue(job)` 把“写库任务”（一个 `job(store)` 函数）放进 `asyncio.Queue`，`sf-writer` 一个个取出来 `to_thread` 执行 → **所有写串行**。
  `Store` 内部另有 `RLock` + 单一写连接，所以后台任务（对齐、清理、旧档导入）直接在线程里调 `Store` 的写方法也是安全的。
  `main.py` 记录写库任务的 `done / failed / busy_since / last_fail`，页面顶部横幅用它判断“写库卡住 / 失败”。
- **读**：`Store._read_conn()` 每个线程一个只读连接（`file:…?mode=ro`），`handle_xxx` 经 `_threaded`（`asyncio.to_thread`）执行。
- **`Recorder` 的锁**：`self._lock`（RLock）保护所有内存表；钩子和 `Attributor` 的包装函数都会碰它。持锁时间很短，不做 IO。
- **离线测试**：没调 `set_submit` 时 `_enqueue` 直接同步执行 `job(store)`，所以测试里不用起事件循环。

### 3.3 生命周期（`main.py`）

`__init__`：建数据目录 → `DiagLog` → `Store` → 找 AstrBot 的 `data_v4.db`（`context.get_db().db_path`，退一步按数据目录找）→ `NameResolver` → `Recorder` → `Attributor` / `Guesser` → `LegacyImporter` → `SessionFlowWebApi`，把诊断 / 配置 / 运行时 / 自检的探针接上。

`initialize()`：建写队列、`recorder.set_submit` → `recover_interrupted`（上次没关的 `running` 运行标成 `interrupted`）→ 注册接口 → 起 4 个后台任务：

| 任务 | 周期 | 做什么 |
|---|---|---|
| `sf-writer` | 常驻 | 从队列取写库任务串行执行，记录成功 / 失败 / 耗时 |
| `sf-watchdog` | 30 s | `watchdog_interrupt_stale`：`last_event_at` 超过 `stale_close_minutes` 的运行标成 `interrupted` |
| `sf-align` | 60 s | `align_provider_stats`：从宿主 `provider_stats` 补 token / 首 token 时间 / 失败状态（`link_provider_stats` 开着才做） |
| `sf-retention` | 24 h | 按 `retention_days` 清理旧运行（0 = 不清） |

`terminate()`：`Attributor.restore()` 把包装拆掉 → 取消后台任务 → 把队列里剩下的写完 → 关库。

---

## 4. 钩子与记录器（Recorder）

### 4.1 钩子表

| 钩子（`main.py`） | 优先级 | `Recorder` 方法 | 干什么 |
|---|---|---|---|
| `on_waiting_llm_request` | 默认 | `on_waiting` | 记“收到时间” `received_at`（聚合等待之后、开始处理之前） |
| `on_llm_request` | **+999999**（最先） | `on_request_first` | 记 `prep_at`；**拍第一张快照**（别的插件改之前的 system prompt / prompt / 额外文本块）；`Attributor.ensure_wrapped()` 在低优先级的处理函数跑之前把它们包好 |
| `on_llm_request` | **−999999**（最后） | `on_request_last` | 此时请求已被所有插件改完：**建 `RunState`**，算注入 / 改动，立即落库一条 `running` 的运行 + 第 1 个 `user` 事件 + 第 2 个 `ctx` 事件 |
| `on_agent_begin` | 默认 | `on_agent_begin` | 记 `begin_at`，在 `run_context.messages` 里找本轮 user 消息的位置（`begin_anchor`），从中拆出追加在提问里的注入块 |
| `on_using_llm_tool` / `on_llm_tool_respond` | 默认 | `on_tool_start` / `on_tool_end` | 记工具开始 / 结束的时间点和结果 |
| `on_llm_response` | 默认 | `on_llm_response` | 每次模型响应打一个点（时间、usage、model） |
| `on_agent_done` | 默认 | `on_agent_done` | **正常结束**：从 `run_context.messages` 切出本轮新增消息，推出步 / 工具 span，对齐时间，关闭运行，落库 |
| `on_decorating_result` | **−999999** | `on_decorating` + `on_sent` | ① 没走到 `agent_done` 但结果不是 LLM 结果 → **模型失败**，关成 `error`；② 把“实际要发出的内容”和模型原话比，不一样才补一条 `sent`，并总是补一条 `timing(after)` |
| `on_plugin_error` | 默认 | `on_plugin_error` | 别的插件处理函数报错 → 记成这一轮的 `error`（没有运行就记一条 `source='note'` 的运行） |

> 为什么 `sent` 在 `on_decorating_result` 里补而不是 `on_after_message_sent`：流式输出时宿主**不触发**发送后钩子（见第 14 节）。

### 4.2 一轮运行的生命

```
on_waiting           received_at[eid]
on_request_first     prep_at[eid]、first_req[eid]；ensure_wrapped()
  （别的插件的 on_llm_request 依次跑，被包装后各自报 handler_notes / 耗时）
on_request_last      建 RunState（run_id = "<trace span_id>#<序号>"）
                     ├─ 立即落库：运行行(status=running) + user 事件(seq=1) + ctx 事件(seq=2)
                     └─ 同 umo 里还有没关的旧运行 → 标 interrupted（新一轮打断旧一轮）
on_agent_begin       begin_at、begin_anchor、提问里的注入块
on_tool_start/end    raw_tool_starts / raw_tool_ends
on_llm_response      response_ticks
on_agent_done        ──► 关闭 status=done，落库 events（seq 接在已有事件后面）
   或                    ──► on_decorating 判断模型失败 → status=error（_fail_close）
   或                    ──► 看门狗超时 → interrupted；进程重启 → recover_interrupted
on_decorating(最后)  on_sent：补 timing(after)，内容有变再补 sent
```

要点（改这里前请理解为什么）：

- **“步”（一次 LLM 调用）在 `agent_done` 时才从新增的 assistant 消息推出**（一条 assistant 一步）；`on_llm_response` 只记时间戳和 usage，到 `agent_done` 再按顺序对齐到步。
- **工具 span 以 assistant 消息里的 `tool_calls` 为准**，再用钩子的 start / end 按“工具名 + 出现顺序”配对（钩子不给 call_id）。报错的工具没有 `tool_end`，结束时间留空；有 start 没配上 span 的补成报错 span。
- **只认同一个事件对象**：`_get_open` 用 `id(event)` 找运行。同群里别的事件（比如一条指令回复的 `decorating`）不能顺着 umo 摸到正在跑的运行把它误关成失败。事件 id 可能被回收复用，所以待补的 `closed_by_eid` 同时存了 `weakref`，用 `ref() is event` 核对。
- **运行关闭和落库解耦**：`_close` 只在内存里收尾，真正写库是 `_flush_closed` 这个 job（在写线程里），事件序号用 `store.max_event_seq(run_id)+1` 接着往下排——因为 open 时已经占了 seq 1、2，后面还会有 `on_sent` 追加的事件。
- **体积构成在写库线程里算**：钩子里只拷一份 `contexts` 列表引用（`_ctx_input`），真正数字数在 `_persist_open` 的 job 里（`ctxsize.measure`），1M 字约 6 ms。
- **临时表 10 分钟清扫**：`received_at / prep_at / first_req / handler_notes / pre_calls / closed_by_eid` 都是按事件 id 暂存的，`_sweep_temp` 清掉 10 分钟前的，防止漏钩子时无限涨。
- **同一个 span 的序号**：`run_id = <span_id>#<n>`，同一条消息里可能有多轮 agent（比如被打断重跑），`n` 递增；运行结束后清掉 `_span_seq`。

### 4.3 状态与来源

| `runs.status` | 含义 |
|---|---|
| `running` | 正在跑（`active_runs()` 给页面轮询） |
| `done` | `agent_done` 正常结束 |
| `error` | 模型失败（`on_decorating` 判定，或 `provider_stats` 对齐时补标）/ `note` 运行 |
| `interrupted` | 同会话新一轮打断 / 看门狗超时 / 重启时没关 |
| `legacy` | 旧档导入的 |

| `runs.source` | 含义 |
|---|---|
| `live` | 实时记录 |
| `legacy` | 旧档导入（`run_id = legacy:<cid>:<序号>`；`time_source` 为 `checkpoint` / `provider_stats` / `estimated`） |
| `note` | 没有运行可挂的插件报错（`run_id = note:<随机>`） |

### 4.4 时间字段

`msg_at`（平台消息时间）、`received_at`（收到）、`prep_at`（请求钩子开始）、`begin_at`（agent 开始）、`end_at`（agent 结束）。
**`sort_at` = `msg_at`，没有就用 `received_at`，再没有用当前时间**，列表按它排序；同 `sort_at` 看 `ord`。时间都是 epoch 秒（float）。

### 4.5 token 口径（容易搞错）

- `runs.token_in` 来自宿主 `provider_stats.token_input_other`，是**没命中缓存**的输入，**不是总输入**。
- `runs.token_cached` 来自 `token_input_cached`，命中缓存的输入。
- **总输入 = `token_in + token_cached`**；缓存命中率 = `token_cached ÷ 总输入`。
- token 不是钩子给的，靠 `align_provider_stats` 异步补：只处理 `source='live'`、已关闭、`stats_id` 为空、`begin_at` 在 1 天内的运行；到宿主 `provider_stats` 里找**同 umo、同 conversation_id、`|start_time − begin_at| ≤ 5 秒`、没被别的运行占用**的最近一行。
  极端连发时可能连错，已连上的（`stats_id` 非空）不会再纠正。

---

## 5. 注入归属（Attributor）

**问题**：system prompt / 提问里多出来一段，是哪个插件的哪个处理函数塞的？宿主不告诉你。

**做法**（`sf/attrib.py`），三层，准确度从高到低：

1. **精确**：宿主按优先级逐个调用 `on_llm_request` 的处理函数。会话流在最高优先级的钩子里，把**别的插件登记的处理函数各包一层**；包装在原函数前后各拍一次快照（`system_prompt` / `prompt` / 额外文本块 / 历史指纹 / 工具集 / 图片 / 模型），变了就记在这个插件这个函数头上。
2. **更早就在**：最高优先级钩子拍第一张快照时已经存在的内容 → 不是任何被包装的处理函数加的，只能是 AstrBot 内核，或优先级比会话流还高的插件。
3. **疑似**（`Guesser`）：到已装插件 / 内核源码里找这个标签名（`<名字>`）的字面量，只给候选（最多 3 个），页面标“疑似”，**读接口时现算、不入库**。

**覆盖的钩子**（`HOOKS` 表）：

| 钩子 | 模式 | 拍什么、记什么 |
|---|---|---|
| `on_llm_request` | `request` | 注入（`system_prompt` / `prompt` / 额外文本块里多出来的）+ 改动（历史 / 工具集 / 图片 / 模型被谁改了） |
| `on_llm_response` | `response` | `completion_text` 被谁改写 |
| `on_decorating_result` | `result` | 发出前的消息链被谁改写 |
| 其余 6 个 | 无 | 只统计次数和耗时 |

**每次调用都计时**（不管有没有改东西）：包装函数在 `finally` 里算耗时，经 `Recorder.add_handler_timing` 汇成每轮的 `timing` 事件。

**和宿主插件管理器的配合（改这里最容易出事）**：宿主（重）载插件时会做

```python
raw = handler.handler.func if isinstance(handler.handler, functools.partial) else handler.handler
handler.handler = functools.partial(raw, 插件实例)
```

所以**包装必须保持 `functools.partial(包装函数, 实例)` 的形状**：`partial.func` 是我们的 `wrapper`，它里面再调原函数。这样宿主重载时会重新绑定新实例，不会调到旧实例，也不会叠包装。`ensure_wrapped()` 每次请求都调一次（已包的直接跳过；会话流自己重载过留下的旧包装会拆开重包）；`restore()` 在 `terminate` 时把包装拆掉。

**注入块怎么切**（`extract.diff_injections`）：base（拍快照时的原文）和 final（处理函数跑完后的文本）比：final 包含 base → base 前面多的算“前置”、后面多的算“追加”；否则取最长公共前后缀，中间新增的算“改写”（被换掉的原文放 `removed`）。新增的文本再按 `<Tag>…</Tag>` 拆成块，块名取标签名，没有标签的用“系统提示词追加”之类的兜底名。
两个处理函数连续追加无标签文字时，首尾快照的差是合成的一整块，和任何一个处理函数记的那段都不相等——所以先 `subtract_texts` 把已有人认领的片段减掉，剩下的才记“未归属”（v0.1.7 修过的重复记项）。

**降级**：非公开接口取不到 → `status.available=False` 并记原因，`Attributor` 安静退出，只剩第 2、3 层；`available=None` 表示“还没有请求经过”（第一次请求才包装），**不是故障**。

---

## 6. 存储（Store）

`sf/store.py`，SQLite（WAL，`synchronous=NORMAL`）。库文件在 AstrBot 数据目录下的 `plugin_data/astrbot_plugin_session_flow/session_flow.db`，同目录 `logs/session_flow.log` 是运行日志。

### 6.1 表

| 表 | 内容 |
|---|---|
| `meta` | 键值：`schema_version`、`export_mask_rules`（屏蔽规则 JSON）等 |
| `blobs` | system prompt 全文：`hash`（sha256）为主键，`data` 是 zlib 压缩的 UTF-8，**相同内容只存一份**；`runs.sysprompt_hash` 指向它 |
| `umos` | 会话卡片：显示名、首 / 末时间、回合数、工具数、报错数、token 合计、最近一句等；**聚合值只由 `recompute_umo` 维护** |
| `runs` | 一行一个回合（字段见 `RUN_COLUMNS`）；正文三件 `user_text / reply_text / error_text` 在列表接口里只取前 300 字 |
| `events` | 一行一个事件：`run_id, seq, t, kind, step, name, call_id, text, data(JSON), is_error, dur_ms` |
| `events_fts` | FTS5（`tokenize='trigram'`，external content 指向 `events.text`），靠触发器同步；建不出来就 `Store.fts=False`，检索退回 `LIKE` |

索引：`runs(umo, sort_at, ord)`、`runs(status)`、`runs(cid)`、`runs(stats_id)`、`events(run_id, seq)`、`events(kind, name)`、`events(run_id, kind, name)`（后者是 runs 列表每轮查 `tools_used` 用的覆盖索引，没有它优化器会选 `kind` 索引扫全表）。

### 6.2 约定

- **不改库结构就能加新数据**：新数据用新的事件 `kind`，内容放 `data` JSON。v0.1.8 的 `ctx`（体积构成）和 `timing`（时间拆分）就是这样加的，没动表结构。`text` 留空的事件不进全文索引。
- 现在**没有迁移框架**：`SCHEMA_VERSION = "1"`，建表全是 `IF NOT EXISTS`。如果以后要加列，建议在 `_init_schema` 里用 `PRAGMA table_info` 判断后 `ALTER TABLE ... ADD COLUMN`，并在 `meta` 里升 `schema_version`；旧版本插件读新库时多出来的列不会出错（用的是按列名取值）。
- **保留期**：`purge(days)` 删 `sort_at` 早于 N 天前的运行（连带事件、孤立的 blob），重算受影响的 `umos`，最后 `VACUUM`。默认 90 天，配置里 0 = 永久保留。
- 旧版本插件读含 `ctx` / `timing` 事件的库不会出错，但时间线会把它们当未知种类显示成空事件。

---

## 7. 事件种类与 data 约定

`events.kind` 一共 12 种。**时间线上显示的** 10 种：

| kind | `name` | `text` | `data`（JSON） |
|---|---|---|---|
| `user` | — | 用户原话（`seq=1`；追加在提问里的注入块已拆出去） | `images`（图片数）、`mid_run`（运行中途插进来的用户消息）、截断信息 |
| `text` | — | 模型回复 | `llm_ms` |
| `think` | — | 思考内容 | `llm_ms` |
| `tool_call` | 工具名 | 参数（美化过的 JSON） | — ；`call_id`、`dur_ms` 在列里 |
| `tool_result` | 工具名 | 结果 | 截断信息；`is_error`、`call_id`、`dur_ms` 在列里 |
| `inject` | 注入块名 | 注入块全文 | `source`（`system_prompt` / `prompt` / `extra_part`）、`attrib`（`exact` / `earlier` / `unattributed`）、`plugin`、`handler`、`priority`、`removed`（被换掉的原文）、`gone`（后来又被别的处理函数改掉了） |
| `edit` | `历史` / `工具集` / `图片` / `模型` | 改动描述（如 `历史 20 → 2 条`） | `attrib=exact`、`what`、`plugin`、`handler`、`priority` |
| `sent` | `实际发出` | 实际发出的文字 | `attrib`、`rewrites[]`（`hook / plugin / handler / before / after / cleared`） |
| `error` | 报错来源 | 报错文字 | — ；`is_error=1` |
| `note` | — | 运行中的 system 消息 | — |

**不在时间线上、由接口拆出来算成两块的** 2 种（`text` 为空）：

| kind | `data` |
|---|---|
| `ctx` | `ctxsize.measure` 的输出：`v / n_msgs / checkpoints / roles{角色: {n, cjk, other, tool_calls}} / images / tool_calls / top[] / system_prompt / prompt / extras / tools / request_images / ms`；量不出来时是 `{v, error}` |
| `timing` | `phase="run"`：`msg_at / received_at / prep_at / req_last_at / begin_at / end_at / tool_ms / calls[]`；`phase="after"`：`agent_done_at / decorating_at / calls[]`。`calls[]` 是别的插件处理函数的 `{hook, plugin, handler, n, total_ms, max_ms}` |

`seq` 的分配：`1 = user`，`2 = ctx`（有的话），之后 `_flush_closed` 接着排（注入、改动、时间线、中途 user、note、error、`timing(run)`），`on_sent` 再接着追加 `timing(after)` 和 `sent`。

---

## 8. 功能模块

下面每个模块都是**可离线测试的纯逻辑**，输入输出见各自文件顶部的 docstring。这里只写它做什么和关键取舍。

### 8.1 `extract.py` — 消息解析（纯函数）

- `model_obj`：pydantic / dataclass / dict 统一成 dict。
- `split_user_content`：本轮 user 消息 → (原话, 注入块列表, 图片数)。列表内容：第一个 text 块是原话，其余 text 块算注入；字符串内容：按 `<Tag>…</Tag>` 拆。
- `assistant_segments` / `tool_calls_of` / `tool_result_text`：拆 assistant 消息和工具结果；`is_error_result_text` 判断工具结果是不是报错。
- `diff_injections` / `subtract_texts`：见第 5 节。
- `result_sig` / `sent_signature` / `chain_text`：把消息链做成签名，比“实际发出”和“模型原话”是否一样（忽略空白、@ / 引用 / 表情等软组件）。
- `ctx_fingerprint` / `diff_contexts` / `diff_names`：历史指纹 / 工具集差分，给“改动”事件用。

### 8.2 `ctxsize.py` — 上下文体积构成（v0.1.8）

- **量的**（`measure`，记录时在写库线程里做）：对 `req.contexts` / system prompt / 提问 / 额外块 / 工具定义数**条数和字数**；中文等宽字符（UTF-8 ≥ 3 字节）和其余字符分开数——用三次整串编码长度（utf-8、ascii-ignore、utf-16-le）**解方程**得到两类字数，不逐字符循环（1M 字约 6 ms）；另数助手工具调用数、图片数、最长 5 条。
  `_checkpoint`（AstrBot 内部的存档标记，不发给模型）只计个数 `checkpoints`，不算大小。
- **估的**（`estimate`，页面 / 导出时现算，不入库）：中文权重 1.0、其余 0.3，各部分算“加权字数”，再用服务商报的总输入 token 求换算系数 `k`，把各部分折算成 token——**各行之和 = 服务商总数**（准），**各行占比是估算**（页面标“估算”）。`k` 不在 0.5～2 时写明“差得比较多”；没有服务商总数就只给字数。
- 工具定义靠 `func_tool.openai_schema()` 序列化后数；宿主没有这个方法就记 `tools=None`，页面写“没数到”。图片只数张数，不算 token。

### 8.3 `timing.py` — 每轮时间拆分（v0.1.8）

只做减法：用运行表里的时间点 + `timing` 事件，切成 **聚合等待（`received − msg`）→ 内核准备（`prep − received`）→ 请求钩子（`req_last − prep`）→ 起跑（`begin − req_last`）→ 模型（`end − begin − tool_ms`）→ 工具 → 回复后处理（`decorating − end`）**。缺哪个时间点就少哪一段；负值一律按 0 算，其中“聚合等待”为负时额外写一条“平台和本机时钟有差”的说明（`msg_at` 是平台时间戳，和本机时钟有差）。没有 `phase="after"` 的计时事件（没走到发送前）就不画“回复后处理”并说明原因。

### 8.4 `health.py` — 顶部横幅自检（v0.1.8）

`evaluate(runtime, hooks, now)` → `[{code, level, title, text}]`，**空列表 = 一切正常，页面不显示横幅**。会亮的：注入归属 `available === false`（warn）；后台任务已停（error）；最近半小时钩子报过错（error，靠 `DiagLog` 的 `last_error_ts`）；写库队列 > 200、单个写库任务卡 > 60 s、最近半小时写库失败（error）。`available: null` 不算故障。

### 8.5 `stats.py` — 按插件统计注入量（v0.1.8）

直接统计已有的 `inject` 事件：按来源插件（精确归属）/ “内核 / 更早” / “未归属” / “疑似 X”分组，给轮数、项数、总字数、平均、出现率、占比、位置分布（`source`）；另附“改动”统计（谁改了历史 / 工具集 / 图片 / 模型，历史一共删了多少条，从 `历史 A → B 条` 文本里解析）。优先用 SQLite `json_extract`，没有就退回 Python 解析。只按字数，不换算 token。

### 8.6 `export.py` + `mask.py` — 导出与屏蔽

- 数据来源是 `webapi._run_payload`（`{run, events, sysprompt, context, timing}`），`export.build` 组装成 bundle，`to_markdown` / `to_json` 渲染。会话 ID / 发送者 ID 默认打码成 `平台:类型:#哈希`。
- **批量导出**（`render_many`）：按时间从早到晚，开头目录，每轮一章；**相同的 system prompt 按 hash 只放一份在附录**；有字数上限 `MAX_BATCH_CHARS = 3_000_000`，超了后面的轮不放并写明；JSON 格式 `session_flow_runs/1`。
- **屏蔽**（`mask.py`）：规则 `{kind, name, on}`（`kind ∈ system_prompt / user / text / think / tool / inject / edit / sent`；`name` 匹配注入块名 / 来源插件 / 处理函数 / 工具名 / 改写者，不分大小写、支持 `*`）；存 `meta` 表键 `export_mask_rules`，最多 100 条；**默认没有规则 = 什么都不屏蔽**；屏蔽在后端做，被屏蔽的段**整段删、不留占位**，文件头写一句“有 N 段被屏蔽”；被屏蔽的注入也从 system prompt / 提问文本里一并抹掉。导出参数 `mask=0|1`、`mask_kinds=a,b`（这一次再整种屏蔽，不入库）。

### 8.7 `spdiff.py` — system prompt 对比上一轮（v0.1.9）

输入上一轮 / 这一轮的 system prompt 全文 + 两轮各自记到的、来自 system prompt 的注入块，输出改动处列表（每处带“谁改的”）和可选的逐行结果。

1. **按行 `difflib`**（`autojunk=False`，免得空行 / 重复行被当垃圾丢掉）。换行统一成 `\n` 再比。
2. 替换块里：行数相等的**按位置配对**；不等的按相似度顺序往下配（`_sim`：短行用 `ratio`，> 400 字的长行只用字符构成的 `quick_ratio ≥ 0.8`，免得整块被当成“全删 + 全加”；块太大 `old×new > 2500` 就不配）。
3. 配上的行再**按“词”对一遍**（`inline`）：数字连续算一个词、字母连续算一个词、汉字单个算一个；夹在两处改动之间只有一两个字的“没变”并进去（`23:51 → 24:03` 框成一块）；两边相似度 < 0.4 就整行当替换。结果是 `[(kind, text)]`，`0` 没变、`1` 新增 / 改后（页面画红实线框）、`2` 被删（红虚线 + 划掉）。
4. 一个替换块按“配上的 / 只删的 / 只加的”拆成各自一处；相邻两处**来源签名相同且非空**就并回一处。
5. **谁带来的**（`attribute`）：拿改动的行去注入块文本里找（行至少 3 个字且含字母 / 数字 / 汉字才算，纯 `---` 不找）；命中数不到最多的 3 成的来源当撞上通用句子丢掉；一处最多列 4 个。新增 / 改后的行用**这一轮**的注入块，纯删除的行用**上一轮**的注入块（写“上一轮由 … 加的，这一轮没了”）；哪个块都不含 → `earlier`。
6. 上限：任一边 > 60 万字或 > 2 万行不逐行比（`too_big`）；最多给 300 处写来源（`truncated`）。

接口 `webapi.handle_sysprompt_diff`：上一轮没存提示词时往前最多找 10 轮，对比最近一个存了的，并返回 `prev.skipped`；各种“没法比”的状态见 `state`：`no_prompt / no_prev / same / changed`。`lines=1` 才带逐行结果（给“完整提示词”视图用，概要接口不带，省流量）。

### 8.8 `search.py` — 全文检索

语法（空格分隔，多个条件取交集）：`tool:<名>`、`err:` / `err:<文字>`、`user:<文字>`、`umo:<片段>`、自由词（**≥ 3 字走 FTS trigram，否则 `LIKE`**）。两个入口：`search()`（检索页，每个回合最多 3 条命中事件，命中按运行时间从新到旧排）、`search_runs_page()`（会话页“搜本对话”，会话精确限定，按页返回）。`kind` 是内容类型筛选（`KIND_GROUPS`）。

### 8.9 `legacy.py` — 旧档导入

只读 `data_v4.db`，把 `conversations` 的历史对话导成 `legacy` 运行，时间优先用 checkpoint，其次 `provider_stats`，都没有才估算（`time_source` 记着）。后台任务，可查进度，可重复执行（已导入的按 `updated_at` 跳过）；已有同 cid 的 live 运行且用户原话前 200 字 + 回复前 200 字都相同的不重复导入。旧档没有注入、改动、实际发出这些事件。

### 8.10 `diag.py` — 诊断日志与诊断包

- `DiagLog`：包装宿主 logger（照样转给宿主），同时记进**内存环（2000 行）+ 滚动日志文件（1 MB × 4）**；统计每个钩子的次数 / 平均 / 最慢 / 报错 / 最近报错时间；保留最近 300 次钩子轨迹和最近 50 条报错；钩子耗时超过 200 ms 记警告。
- **日志规范**：AstrBot 插件市场要求 logger 只能从 `astrbot.api` 导入，不许用内置 `logging`。所以 `main.py` 把 `astrbot.api.logger` 传给 `DiagLog` 当宿主；`webapi.py` 的报错走 `self.diag`（`_log_exc` / `_log_warn`），不自己建 logger；滚动日志文件是 `_RotatingFile`（手写，**不建 `logging.Logger`**）。`tests/test_rules_static.py` 用 `ast` 扫 `main.py` 和 `sf/*.py`，出现 `import logging` / `getLogger` / `RotatingFileHandler` 就失败。
- `_RotatingFile`：句柄常开、每行写完 `flush`；写满 `max_bytes` 把 `.1 → .2 → …` 依次改名，最老的丢。Windows 下文件被占着、改名失败时，这一回不滚、继续追加，原因记在 `rotate_error`（诊断包 `log_file.rotate_error`）；长到 4 倍上限仍滚不动才清空重写。写盘失败只记进 `file_error`，不影响业务、内存环和宿主日志。
- `guard(name, umo)`：钩子外面的上下文管理器，**吞掉异常**、记次数和耗时、记 traceback。
- `build_bundle`：环境、配置（名字带 key / token / secret / password 的值隐藏）、库状态、运行时状态（后台任务、写队列、注入归属状态）、钩子统计、最近报错、最近 40 轮摘要、日志尾巴。**不含聊天正文**，会话 ID 默认打码（`mask_umo`）。

### 8.11 其他

- `names.py`：umo 显示名，优先级 `umo_aliases.user_alias → auto_name → WebChat 会话 display_name → 群 / 私聊兜底 → umo 原文`，只读宿主库，缓存 60 秒。
- `summary.py`：会话的规则摘要（纯 SQL 统计，不调 LLM）。

---

## 9. 接口（webapi）

`SessionFlowWebApi` 把业务写成 `handle_xxx(args: dict, body: dict) -> dict`（**不依赖 quart**，测试里直接调）；`register()` 再包一层从请求里取参数，注册成 `/<插件名>/api/<名字>`。
返回统一：成功 `{"status": "ok", "data": …}`，失败 `{"status": "error", "message": …}`。读库的 `handle_xxx` 都是同步函数，用 `@_threaded` 装饰成 async；**只有 `legacy/import` 例外**，它只发后台任务，必须留在事件循环里。

页面通过 `AstrBotPluginPage.apiGet("api/<名字>", 参数)` / `apiPost` 调用；宿主侧的完整 URL 形如 `/api/v1/plugins/extensions/astrbot_plugin_session_flow/api/<名字>`。

| 路由 | 方法 | 主要参数 | 返回要点 |
|---|---|---|---|
| `api/overview` | GET | `type=all\|group\|private\|other`、`platform`、`errors`、`sort=recent\|runs\|errors\|tokens`、`q` | `stats / platforms / tool_top / umos`（每个 umo 带 `running`） |
| `api/runs` | GET | `umo`（必填）；`page`+`size`（1～200）= 页码模式，再带 `q` / `kind` = 搜本对话；不带 `page` = 游标模式（`limit`+`before`） | 回合列表（正文截 300 字，带 `tools_used`） |
| `api/run` | GET | `id` | `{run, events, sysprompt, context, timing}` |
| `api/sysprompt` | GET | `hash` | system prompt 全文 |
| `api/sysprompt_diff` | GET | `id`、`lines=1` | 和上一轮的对比（见 8.7） |
| `api/export` | GET | `id`、`fmt=md\|json`、`raw`、`mask`、`mask_kinds` | `{filename, mime, text, …}`（文本在 JSON 里，下载由页面用 Blob 做） |
| `api/export_many` | GET | `umo`、`scope=page\|search`、`page`、`size`、`q`、`kind`、`fmt`、`raw`、`mask`、`mask_kinds` | 同上，另有 `requested / included / skipped` |
| `api/mask_rules` | GET | — | 规则 + 库里见过的名字（输入框提示） |
| `api/mask_rules/save` | POST | `{rules:[{kind,name,on}]}` | 保存后的规则 |
| `api/stats/inject` | GET | `days=7\|30\|0`、`umo` | 按插件统计注入量 |
| `api/health` | GET | — | `{problems:[…]}`，空 = 正常 |
| `api/search` | GET | `q`、`umo`、`limit` | 检索页的命中 |
| `api/active` | GET | — | 正在跑的运行（页面 3 秒轮询） |
| `api/umo/summary` | GET | `umo` | 规则摘要 |
| `api/legacy/import` | POST | `{force}` | 启动旧档导入（后台） |
| `api/legacy/status` | GET | — | 导入进度 |
| `api/admin/stats` | GET | — | 库状态 |
| `api/admin/purge` | POST | `{days}`（>0） | 按天数清理 |
| `api/diag` | GET | `raw`、`lines` | 诊断包（不含正文） |

`_run_payload` 是单轮详情和导出共用的数据组装：把事件里的 `ctx` / `timing` 拆出来，对非精确归属的 `inject` 事件**现算**疑似候选放进 `data.guess`，算出 `sysprompt.same_as_prev`，再算 `context`（体积构成）和 `timing` 两块。

---

## 10. 管理页（pages/session_flow）

原生 JS（一个 IIFE，约 1200 行）+ CSS，没有构建、没有依赖。页面由宿主放进 iframe 里，通过宿主注入的 `/api/plugin/page/bridge-sdk.js` 拿到 `window.AstrBotPluginPage`（`apiGet / apiPost / ready`）。

- **四个标签页**：会话（会话卡片 → 回合列表 → 单轮详情）、检索、旧档、管理。
- **`el(tag, attrs, ...children)`** 是唯一的 DOM 构造小工具（`null / undefined / false` 的子节点会被忽略）。**注意**：原生 `node.append(x)` 遇到 `null` 会写出字面的 `null` 两个字，往原生 `append` 传可能为空的变量时要先过滤（展开数组 `...[]`）。
- **几个硬约束（`tests/test_page_static.py` 锁住了）**：
  - **不能用 `confirm()` / `alert()` / `prompt()`**：插件页 iframe 没有 `allow-modals`，这些调用不弹窗直接返回。需要确认的按钮做成页内两次点击确认（如“清理”要点两次，4 秒内有效）。
  - `localStorage` 每处使用都要在 `try` 里（无痕窗口 / 屏蔽站点数据时会抛）。
  - 页面调用的每个接口、用到的每个元素 id 都要存在（静态检查对账）。
  - 暗色：CSS 变量 + `@media (prefers-color-scheme: dark)` + `[data-theme="dark"]` 两套；窄屏（≤ 760 px）单独排版，页面级不能出横向滚动条。
- **轮询**：会话页可见时每 3 秒刷新一次“运行中”标记；顶部横幅每 30 秒查一次 `api/health`（页面在后台标签页时都不查）。
- **长文本**：全文都在 DOM 里，只是先折叠（`clamp`），点“展开全文”；含检索词的长文本默认展开。
- 详情页的各个区块（时间拆分 `#run-timing`、体积构成 `#run-context`、system prompt 对比 `#sp-block` / `#sp-diff`、system prompt 全文 `#sysprompt-box`、事件 `#events`）都是 `openRun` 里按接口返回逐块渲染。

---

## 11. 配置项

`_conf_schema.json`（**每一项的 `hint` 都要写大白话**：管什么、调大调小会怎样）：

| 键 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `enable_record` | bool | true | 总开关 |
| `exclude_umos` | list | [] | 不记录的会话（支持通配 `fnmatch`） |
| `store_system_prompt` | bool | true | 存 system prompt 全文（关了对比 / 导出就没有提示词） |
| `max_text_chars` | int | 50000 | 单条正文字数上限（system prompt 不受此限） |
| `max_tool_result_chars` | int | 20000 | 单条工具结果字数上限 |
| `retention_days` | int | 90 | 保留天数，0 = 永久 |
| `link_provider_stats` | bool | true | 对齐宿主用量表补 token |
| `stale_close_minutes` | int | 30 | 多久没动静就把运行标成中断 |
| `diag_log_enabled` | bool | true | 写运行日志文件 |
| `diag_log_runs` | bool | true | 日志里逐轮记开始 / 结束 |

---

## 12. 测试

**只用标准库，不用装 AstrBot**（`tests/_stubs.py` 仿了一个最小的 `astrbot` 桩，只仿形状不仿行为）。在插件目录下：

```
python -I tests/run_all.py
```

当前 **293 个用例**，在 Python 3.12 下跑过。`run_all.py` 把临时目录指到 `tests/.tmp`（有的环境系统临时目录里 SQLite 写不进）——**这个目录会越攒越大，不用时可以整个删掉，别提交**（`.gitignore` 已排除）。

| 文件 | 管什么 |
|---|---|
| `test_store.py` | 建表、FTS、触发器、增删改查 |
| `test_extract.py` / `test_inject_diff.py` | 消息解析、注入差分 |
| `test_recorder.py` | 照真实宿主的时间线回放钩子，查库里的时间线 |
| `test_attrib.py` | 包装 / 还原、`partial` 形状、精确归属、疑似 |
| `test_hooks_ext.py` | 回复 / 发出前 / 历史 / 工具集等其他钩子上的改动和耗时 |
| `test_legacy.py` | 旧档导入（用 `tests/data/legacy_fixture.db`，合成数据，由 `make_fixture.py` 生成） |
| `test_search.py` / `test_pager.py` | 检索语法、分页、搜本对话、4 万条大批量回归 |
| `test_export.py` / `test_v018.py` | 单轮 / 批量导出、屏蔽、体积构成、时间拆分、横幅、注入统计 |
| `test_spdiff.py` | system prompt 对比（行内 / 逐行 / 来源 / 接口各状态） |
| `test_summary.py` / `test_webapi.py` / `test_diag.py` | 摘要、接口、诊断（含滚动文件的轮转 / 改名失败兜底、接口报错进诊断日志） |
| `test_main_wiring.py` | `main.py` 的钩子接线、异常被吞 |
| `test_page_static.py` | 页面静态检查（见第 10 节） |
| `test_rules_static.py` | 日志规范：`main.py` / `sf/` 里不许用内置 `logging` |

**测试辅助**：`tests/fakes.py` 有 `FakeEvent / FakeReq / FakeCtx / FakeResp / FakeResult` 和 `drive_run(rec, …)`（按真实时间线驱动一整轮钩子）、`make_recorder()`、`tmp_db_path()`。
几个值得照抄的写法：用 `unittest` + 离线 `Store`；接口用 `asyncio.new_event_loop().run_until_complete(api.handle_xxx(args, body))` 直接调；对算法类模块写“**还原不变量**”（如 `spdiff` 用逐行结果还原出新 / 旧文本必须和原文一致）。

**本地预览**：`python tests/ui_harness.py [端口]` 起一个只监听 127.0.0.1 的小服务器，用 `fakes.drive_run` 现场造回合 + 导入旧档夹具，把 `pages/session_flow/` 当页面服务，把宿主桥接脚本换成 `/mock-bridge.js`（把 `apiGet / apiPost` 转成 `fetch("/x/<接口>")` 再交给 `webapi` 的处理函数）。可以直接用浏览器看，也可以用无头浏览器（Chrome DevTools Protocol）真点——这是改页面时的主要验证手段，因为页面逻辑没有 JS 单测。

---

## 13. 怎么加东西（清单）

**加一个新事件种类**（例如再记一种运行数据）
1. 在 `recorder.py` 的 `_build_events`（运行结束时写）或 `_persist_open`（开运行时写）里 `add(kind="xxx", text="", data={…})`；`text` 留空就不进全文索引；**不用改表**。
2. 在 `webapi._run_payload` 里决定它是进时间线（`events`）还是像 `ctx` / `timing` 那样拆出来算成单独的块。
3. 页面渲染（`eventNode` / 新面板）、导出（`export.py` 的 `KIND_TXT` / `to_markdown`）、屏蔽（`mask.py` 的 `KINDS` / `_EV_TO_RULE`，并同步页面里写死的一份 `MASK_KINDS`）。
4. 补测试：记录（`test_recorder` / `test_v018`）+ 接口 + 导出。

**加一个接口**
1. `webapi.py` 写 `handle_xxx(self, args, body)`（读库的加 `@_threaded`），在 `routes()` 里登记。
2. 页面里用 `api.get("api/xxx", …)` 调用——`test_page_static.test_endpoints_exist` 会核对页面调的接口都已登记。
3. README 的接口表和本文第 9 节同步。

**加页面区块**
1. `index.html` 里放容器（有 id），`app.js` 里用 `$("#id")` 取、`el()` 构造；`test_element_ids` 会核对 `$("#…")` 用到的 id 都存在。
2. CSS 颜色一律用主题变量（`var(--danger)` 等），亮 / 暗两套自动跟着变；窄屏补一段 `@media (max-width: 760px)`。
3. 用本地预览 + 无头浏览器真点一遍，亮 / 暗 / 390 px 窄屏各一遍。

**加一个钩子**
1. `main.py` 加转发（外面套 `self.diag.guard(...)`，**异常绝不外抛**），`Recorder` 加方法（try / except 兜住并 `_log`）。
2. 想统计别的插件在这个钩子上的耗时 / 改动：在 `attrib.HOOKS` 登记 `(EventType 属性名, 短名, 模式)`。
3. 补 `test_main_wiring` 和 `test_recorder` / `test_hooks_ext`。

**改记录逻辑之后**：除了 `run_all.py`，最好在真宿主上冒烟一遍（至少跑几轮对话，看诊断包里钩子 0 报错、写库 `failed=0`、注入归属 `available=true`）。离线桩只仿形状，不能保证宿主行为没变。

**发版**：改 `metadata.yaml` 的 `version`、README、本文的相关节；跑全部用例；打包时排除 `__pycache__` / `tests/.tmp`。

---

## 14. 宿主兼容性与非公开依赖

**最低版本** `astrbot_version >= 4.28.0`。在 **AstrBot 4.28.1**（Windows 便携版，Python 3.12）和 **4.29.0-beta.1**（Linux，napcat）上实测过。

**用到的公开接口**：`astrbot.api` 的 `logger`、`filter.on_waiting_llm_request / on_llm_request / on_agent_begin / on_using_llm_tool / on_llm_tool_respond / on_llm_response / on_agent_done / on_decorating_result / on_plugin_error`、`Star`、`StarTools.get_data_dir`、`context.register_web_api`、`context.get_db()`。

**用到的非公开接口**（宿主改了就可能失效，都有退路）：

| 接口 | 用在哪 | 失效时 |
|---|---|---|
| `astrbot.core.star.star_handler.star_handlers_registry`、`EventType`、`astrbot.core.star.star.star_map`（在 `Attributor._hosts` 里一起 import） | 取各钩子上已登记的处理函数元数据，并由模块路径查插件名 | import 失败 / 接口对不上 → 精确归属、改动、处理函数耗时不可用（`status.available=False`，记下原因），记录照常；只有单个插件名取不到时退回用模块路径推 |
| `astrbot.core.config.default.VERSION` | 诊断包里写 AstrBot 版本 | 写空 |
| `astrbot.core.utils.astrbot_path.get_astrbot_data_path` | 找不到 `context.get_db().db_path` 时按数据目录找 `data_v4.db` | 对齐 token / 旧档导入不可用 |
| 宿主 `data_v4.db` 的表：`provider_stats`、`conversations`、`platform_message_history`、`umo_aliases`、`platform_sessions` | token 对齐、旧档导入、会话显示名（只读） | 对应功能不可用，会话显示名退回兜底 |
| 请求 / 事件对象的字段：`req.system_prompt / prompt / contexts / extra_user_content_parts / func_tool / image_urls / model / conversation.cid`、`event.trace.span_id`、`event.message_obj.timestamp`、`event.get_result()` | 记录 | 单项取不到记空，不抛 |
| `func_tool.openai_schema()` | 体积构成里数工具定义 | 页面写“没数到” |
| 页面桥接 `/api/plugin/page/bridge-sdk.js`、`window.AstrBotPluginPage` | 管理页 | 页面不可用 |

**摸底得到的宿主行为（设计依据，别随意改记录策略）**：

1. **流式输出时 `on_after_message_sent` 不触发** → “实际发出”挂在 `on_decorating_result`，流式中间帧（`STREAMING_RESULT`）不处理，等 `STREAMING_FINISH`。
2. **工具抛异常时 `on_llm_tool_respond` 不触发** → 工具 span 以消息里的 `tool_calls` 为准，没有 end 的标成报错。
3. **模型失败时 `on_agent_done` 不触发** → 靠 `on_decorating_result` 里“结果不是 LLM 结果”判定，并用 `provider_stats` 的 error 行补标。
4. **整轮 token 不在钩子里** → 从 `provider_stats` 对齐。
5. **插件页只认 `pages/`**，iframe 没有 `allow-modals`。
6. 4.28.1 的历史里每个回合多一条 `_checkpoint` 内部标记（`contexts_len` 算上了，但不发给模型）；4.29 上没专门核对。
7. 同一个 `event` 对象贯穿一轮的所有钩子，所以用 `id(event)`（配合 `weakref`）把各钩子的数据串起来。

---

## 15. 已知限制与没做的

- 同一会话前后两轮在 5 秒内开跑时，token 对齐按开始时间最近连，极端连发仍可能连错；已连上的不会自动纠正。
- 旧档导入去重靠“同 cid 已有 live 运行且用户原话前 200 字 + 回复前 200 字相同”，有插件往 user 消息里追加内容时可能对不上而重复导入——宁可重复不误跳。
- 体积构成的 token 占比是按字数折算的**估算**（权重 1.0 / 0.3 只在少量真实数据上看过）；图片 token 没算；最后一个钩子之后宿主若再裁剪历史，量到的会偏多。
- 时间拆分里“聚合等待”含平台时间戳与本机时钟的差；只统计会话流包装得到的别的插件的处理函数，发送后的不计。
- system prompt 对比的“谁改的”只能认出**被包装的处理函数**加进来的内容；内核拼的、优先级比会话流还高的插件改的只会写“更早就在”。
- 历史对话内容（`req.contexts` 里的消息正文）**不存**，只存条数 / 字数构成。
- 页面在离线预览里用无头浏览器点过（亮 / 暗 / 390 px），**没在真 AstrBot WebUI 的 iframe 里系统地点过**；高并发群的开销没压测过。
- 没做：会话表 / 检索页分页、回复被缩成一段后其余分段的“稍后补发”标注、页面里的 hash 路由 / 右侧导航 / 平台下拉。
- 没有 JS 单测；页面逻辑靠静态检查 + 浏览器真点。

---

## 16. 版本历史

| 版本 | 内容 |
|---|---|
| v0.1.0 | 首版：钩子记录器 + SQLite 存档 + 插件页（会话 / 检索 / 旧档 / 管理）+ 旧档导入 |
| v0.1.1 | 运行日志与诊断包（不含聊天正文，会话 ID 打码） |
| v0.1.2 | 把 system prompt / 提问里别的插件塞的内容识别为注入；按事件种类配色、长文折叠、检索高亮 |
| v0.1.3 | **注入归属**：包装别的插件的 `on_llm_request` 处理函数，标出是哪个插件的哪个函数塞的 |
| v0.1.4 | 回合列表分页 + “搜本对话” + 自己滚的列表 |
| v0.1.5 | 包装扩到其他钩子：历史 / 工具集 / 图片 / 模型谁改的、回复 / 发出前谁改写、所有钩子上处理函数的耗时 |
| v0.1.6 | 单轮导出（Markdown / JSON）；诊断包补 `tokens_cached` |
| v0.1.7 | 修“两个处理函数连续追加无标签文字时多记一项未归属” |
| v0.1.8 | 上下文体积构成 + 缓存命中率、每轮时间拆分、顶部横幅、按插件统计注入量、导出整页 / 搜索结果、导出时可选屏蔽 |
| v0.1.9 | system prompt 对比上一轮：详情右上角小块 + 改动处红框的完整提示词视图 + 每处改动标是谁改的 |
| **v1.0.0** | 正式版。代码和 v0.1.9 相同；补上开发文档（`docs/DEVELOPMENT.md`）、`LICENSE`（AGPL-3.0）、`.gitignore`，清掉文档和注释里的本机路径 |
| **v1.0.1** | 按插件市场的日志规范整改：不再使用内置 `logging`（`webapi.py` 的报错改走诊断日志，`diag.py` 的滚动日志文件改为手写的 `_RotatingFile`）；接口报错现在也会进诊断包的“最近报错”；诊断包 `log_file` 多一项 `rotate_error`；新增静态测试锁住日志规范。功能和页面没有变化 |
