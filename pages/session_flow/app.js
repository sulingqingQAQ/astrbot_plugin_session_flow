/* 会话流管理页。原生 JS，无第三方依赖。
 * 通过 AstrBot 注入的 window.AstrBotPluginPage 调用插件后端（sf/webapi.py）。 */
(function () {
  "use strict";

  const bridge = window.AstrBotPluginPage;
  const $ = (sel) => document.querySelector(sel);

  // ------------------------------------------------------------ 小工具
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children.flat()) {
      if (c === undefined || c === null || c === false) continue;
      node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }
  let toastTimer = null;
  function toast(msg, isError) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast" + (isError ? " error" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), isError ? 7000 : 3500);
  }
  async function busy(button, fn) {
    if (button) button.disabled = true;
    try { return await fn(); }
    catch (e) { toast(e.message || String(e), true); return null; }
    finally { if (button) button.disabled = false; }
  }
  const api = {
    get: (ep, params) => bridge.apiGet(ep, params || {}),
    post: (ep, body) => bridge.apiPost(ep, body || {}),
  };
  const fmtTime = (t) => {
    if (!t) return "—";
    try { return new Date(t * 1000).toLocaleString("zh-CN", { hour12: false }); }
    catch (e) { return String(t); }
  };
  const fmtNum = (x) => (x === null || x === undefined ? "—" : String(x));
  const pct = (x) => (x === null || x === undefined || Number.isNaN(x) ? "—" : (x * 100).toFixed(1) + "%");
  const fmtSec = (s) => (s >= 10 ? s.toFixed(1) : s >= 1 ? s.toFixed(2) : s.toFixed(3)) + " s";
  const STATUS_TXT = { running: "运行中", done: "完成", error: "报错", interrupted: "被打断", legacy: "旧档" };

  // ------------------------------------------------------------ 标签页
  const loaded = {};
  function showTab(name) {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
    if (!loaded[name]) {
      loaded[name] = true;
      if (name === "legacy") refreshLegacy();
      if (name === "admin") { loadAdmin(); loadInjectStats(); renderMaskEditor(); }
    }
  }
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  // ------------------------------------------------------------ 会话页
  let overview = null;
  let curUmo = null;
  // 回合列表：页码 + 每页条数（记在浏览器里）+ 本对话搜索
  const SIZES = [20, 50, 100];
  const KIND_FILTER_TXT = { user: "提问", text: "回复", think: "思考", tool: "工具", inject: "注入", edit: "改动", error: "报错" };
  let curPage = 1;
  let curQ = "";
  let curKind = "";
  let pageBeforeSearch = 1;   // 进入搜索前停在第几页，退出搜索时回到那一页
  let runsSeq = 0;            // 防乱序：连点翻页时只认最后一次请求的返回
  function loadSize() {
    try {
      const n = Number(localStorage.getItem("sf.runs.size"));
      return SIZES.includes(n) ? n : 20;
    } catch (e) { return 20; }
  }
  function saveSize(n) {
    try { localStorage.setItem("sf.runs.size", String(n)); } catch (e) { /* 存不了就算了，下次还是 20 */ }
  }
  let curSize = loadSize();

  async function loadOverview() {
    overview = await api.get("api/overview", filterParams());
    renderOverview();
  }
  function filterParams() {
    const p = { q: $("#f-q").value.trim(), type: $("#f-type").value, sort: $("#f-sort").value };
    if ($("#f-errors").checked) p.errors = "1";
    return p;
  }
  function renderOverview() {
    const s = overview.stats;
    $("#brand-counts").textContent = `${s.umos} 会话 · ${s.runs} 回合 · ${s.errors} 报错`;
    const stats = [
      ["会话", s.umos], ["回合", s.runs], ["旧档回合", s.legacy_runs],
      ["工具调用", s.tools], ["报错", s.errors], ["运行中", s.running],
    ];
    clear($("#stats")).append(...stats.map(([l, n]) => el("div", { class: "stat" }, el("div", { class: "num" }, n), el("div", { class: "lbl" }, l))));
    const tt = clear($("#tool-top"));
    if (!overview.tool_top.length) tt.append(el("span", { class: "muted" }, "还没有工具调用"));
    for (const t of overview.tool_top) {
      tt.append(el("span", { class: "chip" }, `${t.name} ×${t.count}`,
        t.errors ? el("span", { class: "badge err" }, `错${t.errors}`) : null));
    }
    $("#umo-count").textContent = `共 ${overview.umos.length} 个`;
    const t = clear($("#umo-table"));
    if (!overview.umos.length) { t.append(el("tr", {}, el("td", { class: "muted" }, "没有会话（过滤条件太严，或还没有记录到任何回合）"))); return; }
    t.append(el("tr", {}, el("th", {}, "会话"), el("th", {}, "回合"), el("th", {}, "报错"), el("th", {}, "最近")));
    for (const u of overview.umos) {
      const name = u.display_name || u.umo;
      t.append(el("tr", { class: "clickable" + (curUmo === u.umo ? " sel" : ""), onclick: () => openUmo(u.umo) },
        el("td", {}, u.running ? el("span", { class: "badge ok" }, "跑") : null, " ", name,
          el("div", { class: "muted" }, el("code", {}, u.umo))),
        el("td", {}, u.run_count),
        el("td", {}, u.error_count ? el("span", { class: "badge err" }, u.error_count) : "0"),
        el("td", { class: "muted" }, fmtTime(u.last_at))));
    }
  }
  $("#btn-filter").addEventListener("click", (e) => busy(e.target, loadOverview));
  $("#f-q").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); loadOverview().catch((err) => toast(err.message || err, true)); } });

  async function openUmo(umo) {
    curUmo = umo;
    curPage = 1;
    curQ = "";
    curKind = "";
    pageBeforeSearch = 1;
    $("#rs-q").value = "";
    $("#rs-kind").value = "";
    $("#rs-clear").classList.add("hidden");
    $("#pg-size").value = String(curSize);
    resetBatchNote();
    $("#runs-empty").classList.add("hidden");
    $("#umo-detail").classList.remove("hidden");
    $("#run-detail-card").classList.add("hidden");
    clear($("#runs-list"));
    $("#summary-result").classList.add("hidden");
    renderUmoSel();
    await loadRuns(1);
    revealRunsCard();
  }
  // 点左边的会话后：回合列表（搜索框 + 分页条 + 滚动框）整块不在视野里就把卡片滚到顶，一屏看全
  function revealRunsCard() {
    try {
      const card = $("#runs-card").getBoundingClientRect();
      const box = $("#runs-list").getBoundingClientRect();
      if (card.top < 0 || box.bottom > window.innerHeight) $("#runs-card").scrollIntoView({ block: "start" });
    } catch (e) { /* 量不了位置就不滚 */ }
  }
  function renderUmoSel() {
    document.querySelectorAll("#umo-table tr").forEach(() => {});
  }
  // 按页码取：新的在上。有搜索词 / 内容类型时是“搜本对话”，返回的是命中的回合
  async function loadRuns(page) {
    const umo = curUmo;
    const seq = ++runsSeq;
    const params = { umo, page: page || 1, size: curSize };
    if (curQ) params.q = curQ;
    if (curKind) params.kind = curKind;
    $("#runs-list").classList.add("loading");
    const r = await busy(null, () => api.get("api/runs", params));
    if (seq !== runsSeq || umo !== curUmo) return;   // 期间又翻了页 / 换了会话，这份作废
    $("#runs-list").classList.remove("loading");
    if (!r) return;
    curPage = r.page;
    renderRuns(r);
    loadOverviewQuiet();
  }
  function renderRuns(r) {
    $("#runs-title").textContent = r.umo.display_name || r.umo.umo;
    const kindTxt = curKind ? `（仅${KIND_FILTER_TXT[curKind] || curKind}）` : "";
    $("#umo-summary").textContent = r.searching
      ? `搜${curQ ? "“" + curQ + "”" : ""}${kindTxt}：命中 ${r.total} 轮`
      : `共 ${r.total} 轮`;
    const box = clear($("#runs-list"));
    const terms = searchTerms(curQ);
    if (!r.runs.length) {
      box.append(el("div", { class: "muted" }, r.searching ? "没有命中，换个词或换个内容类型试试。" : "这个会话还没有回合。"));
    } else {
      box.append(...r.runs.map((run) => runNode(run, terms)));
    }
    box.scrollTop = 0;
    $("#pg-pages").textContent = r.pages;
    $("#pg-total").textContent = r.searching ? `命中 ${r.total} 轮` : `共 ${r.total} 轮`;
    const jump = $("#pg-jump");
    jump.value = r.page;
    jump.max = r.pages;
    $("#pg-prev").disabled = r.page <= 1;
    $("#pg-next").disabled = r.page >= r.pages;
    $("#pg-size").value = String(r.size);
    $("#btn-bx-search").classList.toggle("hidden", !r.searching);
    $("#btn-bx-page").textContent = r.searching ? "导出本页（搜索结果）" : "导出本页";
  }
  $("#pg-prev").addEventListener("click", () => { if (curPage > 1) busy(null, () => loadRuns(curPage - 1)); });
  $("#pg-next").addEventListener("click", () => busy(null, () => loadRuns(curPage + 1)));
  $("#pg-size").addEventListener("change", (e) => {
    curSize = SIZES.includes(Number(e.target.value)) ? Number(e.target.value) : 20;
    saveSize(curSize);
    busy(null, () => loadRuns(1));
  });
  // 页码框：改完回车（或离开输入框）就跳；超出范围服务端收到最后一页
  $("#pg-jump").addEventListener("change", () => {
    const n = Math.floor(Number($("#pg-jump").value));
    if (n >= 1) busy(null, () => loadRuns(n));
    else $("#pg-jump").value = curPage;
  });

  // ---- 搜本对话
  function applySearch() {
    const q = $("#rs-q").value.trim();
    const kind = $("#rs-kind").value;
    if (!q && !kind) { clearSearch(); return; }
    if (!curQ && !curKind) pageBeforeSearch = curPage;
    curQ = q;
    curKind = kind;
    $("#rs-clear").classList.toggle("hidden", !$("#rs-q").value);
    busy($("#rs-go"), () => loadRuns(1));
  }
  function clearSearch() {
    $("#rs-q").value = "";
    $("#rs-kind").value = "";
    $("#rs-clear").classList.add("hidden");
    if (!curQ && !curKind) return;
    curQ = "";
    curKind = "";
    busy(null, () => loadRuns(pageBeforeSearch));
  }
  $("#runs-search").addEventListener("submit", (e) => { e.preventDefault(); applySearch(); });
  $("#rs-kind").addEventListener("change", applySearch);
  $("#rs-clear").addEventListener("click", clearSearch);
  $("#rs-q").addEventListener("input", () => $("#rs-clear").classList.toggle("hidden", !$("#rs-q").value));
  $("#rs-q").addEventListener("keydown", (e) => { if (e.key === "Escape") clearSearch(); });
  // 刷新左侧列表的 running 标记，不动已选中的回合
  async function loadOverviewQuiet() {
    try {
      const o = await api.get("api/overview", filterParams());
      overview = o;
      renderOverview();
    } catch (e) { /* 安静失败 */ }
  }
  // 搜索时卡片下面列出命中的事件片段；提问 / 回复的片段如果卡片上已经整段显示了就不重复列
  function hitRowsOf(run, terms) {
    const shown = { user: run.user_text || "", text: run.reply_text || "" };
    const rows = [];
    for (const h of run.hits || []) {
      const core = (h.snippet || "").replace(/^…|…$/g, "");
      if (shown[h.kind] !== undefined && core && shown[h.kind].includes(core)) continue;
      rows.push(el("div", { class: "hit-row" }, kindChip(h.kind, h.name, h.is_error),
        el("span", { class: "snip" }, highlight(h.snippet, terms))));
    }
    if (rows.length && run.hits_more) rows.push(el("div", { class: "hit-more" }, "这一轮还有更多命中，点开看整轮"));
    return rows;
  }
  function runNode(run, terms) {
    terms = terms || [];
    const hitRows = hitRowsOf(run, terms);
    const hitSeqs = (run.hits || []).map((h) => h.seq);
    const statusTxt = STATUS_TXT[run.status] || run.status;
    const badges = [el("span", { class: "badge" }, statusTxt)];
    // 去重：“旧档”状态和“旧档”来源只显示一次
    if (run.source === "legacy" && statusTxt !== "旧档") badges.push(el("span", { class: "badge" }, "旧档"));
    else if (run.source && run.source !== "live" && run.source !== "legacy") badges.push(el("span", { class: "badge" }, run.source));
    if (run.n_tools) badges.push(el("span", { class: "badge tool" }, `工具×${run.n_tools}`));
    const tokTotal = (run.token_in || 0) + (run.token_cached || 0);
    if (run.token_cached !== null && run.token_cached !== undefined && tokTotal > 0) {
      const rate = run.token_cached / tokTotal;
      badges.push(el("span", {
        class: "badge cache-badge" + (rate < 0.5 ? " low" : ""),
        title: `输入合计 ${tokTotal} token：命中缓存 ${run.token_cached}，没命中 ${run.token_in}`,
      }, `缓存 ${(rate * 100).toFixed(0)}%`));
    }
    if (run.n_errors) badges.push(el("span", { class: "badge err" }, `错×${run.n_errors}`));
    return el("div", { class: "run", onclick: (e) => openRun(e.currentTarget, run.run_id, terms, hitSeqs) },
      el("div", { class: "q" }, highlight(run.user_text || "（空提问）", terms)),
      run.reply_text ? el("div", { class: "a" }, highlight(run.reply_text, terms)) : null,
      run.error_text ? el("div", { class: "a" }, "⚠ " + run.error_text) : null,
      hitRows.length ? el("div", { class: "hits" }, hitRows) : null,
      el("div", { class: "meta" }, el("span", {}, fmtTime(run.sort_at)),
        (run.tools_used || []).map((n) => el("span", { class: "badge tool" }, n)),
        badges));
  }
  $("#btn-full-summary").addEventListener("click", (e) => busy(e.target, async () => {
    const s = await api.get("api/umo/summary", { umo: curUmo });
    const box = $("#summary-result");
    box.classList.remove("hidden");
    box.textContent = JSON.stringify(s, null, 2);
  }));
  $("#btn-active").addEventListener("click", (e) => busy(e.target, async () => {
    const r = await api.get("api/active");
    const mine = r.runs.filter((x) => x.umo === curUmo);
    toast(mine.length ? `这个会话正在跑 ${mine.length} 轮：` + mine.map((x) => x.run_id).join("、") : "这个会话现在没有正在跑的回合");
  }));

  // ------------------------------------------------------------ 单轮导出
  // 后端把一轮的全部内容（正文、system prompt 全文、注入全文和归属）整理成一个文件的内容，
  // 这里只负责存成文件 / 复制；复制不了就把全文放进文本框让人自己全选。
  let curRunId = null;
  const EX_NOTE = "导出的文件含这一轮的聊天正文和 system prompt，会话 ID 默认打码，正文不脱敏，发出去前自己看一眼。";
  function resetExportNote() {
    $("#ex-note").textContent = EX_NOTE;
    const fb = $("#ex-fallback");
    fb.value = "";
    fb.classList.add("hidden");
  }
  // 屏蔽规则存在插件库里（管理页编辑）；导出栏只负责“这一次用不用”和“这一次再多屏蔽哪几种”
  const MASK_KINDS = [["system_prompt", "system prompt"], ["user", "提问"], ["text", "回复"], ["think", "思考"],
    ["tool", "工具"], ["inject", "注入"], ["edit", "改动"], ["sent", "实际发出"]];
  let maskInfo = { rules: [], kinds: [], seen: {} };
  const enabledRules = () => (maskInfo.rules || []).filter((r) => r.on !== false);
  function buildMaskBox(box, prefix) {
    clear(box);
    const n = enabledRules().length;
    const cb = el("input", { type: "checkbox", id: prefix + "-mask" });
    cb.checked = n > 0;
    cb.disabled = n === 0;
    box.append(el("label", { class: "check", title: n ? "取消勾选 = 这一次不用屏蔽规则" : "还没有屏蔽规则，到「管理」页添加" },
      cb, n ? ` 按屏蔽规则屏蔽（${n} 条）` : " 屏蔽规则（还没有）"));
    box.append(el("details", {}, el("summary", {}, "这一次再屏蔽…"),
      el("div", { class: "once-list" }, ...MASK_KINDS.map(([k, txt]) => el("label", { class: "check" },
        el("input", { type: "checkbox", class: prefix + "-once", "data-kind": k }), " " + txt)))));
  }
  function buildMaskBoxes() {
    buildMaskBox($("#ex-mask-box"), "ex");
    buildMaskBox($("#bx-mask-box"), "bx");
  }
  async function loadMaskRules() {
    try { maskInfo = await api.get("api/mask_rules"); } catch (e) { /* 取不到就当没有规则 */ }
    buildMaskBoxes();
    renderMaskEditor();
  }
  function readOpts(prefix) {
    const mask = $("#" + prefix + "-mask");
    const kinds = [...document.querySelectorAll("." + prefix + "-once")].filter((c) => c.checked).map((c) => c.dataset.kind);
    return { fmt: $("#" + prefix + "-fmt").value, raw: $("#" + prefix + "-raw").checked ? "1" : "0",
      mask: mask && mask.checked ? "1" : "0", mask_kinds: kinds.join(",") };
  }
  async function fetchExport() {
    if (!curRunId) { toast("先点开一轮", true); return null; }
    return api.get("api/export", { id: curRunId, ...readOpts("ex") });
  }
  const maskNote = (r) => (r.masked_segments ? `；有 ${r.masked_segments} 段被屏蔽（已整段删掉）` : "");
  function exportNote(r) {
    return `${r.filename}：${r.chars} 字 · 注入 ${r.injects} 项（${r.inject_chars} 字）· system prompt ${r.system_prompt_chars} 字 · ` +
      (r.masked ? "会话 ID 已打码" : "会话 ID 是原始值") + maskNote(r) + "；正文没脱敏，发出去前自己看一眼。";
  }
  // ---- 批量导出：当前页 / 搜索结果
  function resetBatchNote() {
    const n = $("#bx-note");
    n.classList.add("hidden");
    n.textContent = "";
    const fb = $("#bx-fallback");
    fb.value = "";
    fb.classList.add("hidden");
  }
  async function fetchBatch(scope) {
    if (!curUmo) { toast("先点开一个会话", true); return null; }
    const p = { umo: curUmo, scope, page: curPage, size: curSize, ...readOpts("bx") };
    if (curQ) p.q = curQ;
    if (curKind) p.kind = curKind;
    return api.get("api/export_many", p);
  }
  function batchNote(r) {
    const cut = r.skipped ? `；选了 ${r.requested} 轮，超过大小上限，只放了前 ${r.included} 轮，后面 ${r.skipped} 轮没放进来` : "";
    return `${r.filename}：${r.scope}，${r.included} 轮 · ${r.chars} 字 · system prompt ${r.system_prompts} 份（相同的只放一份）· ` +
      (r.masked ? "会话 ID 已打码" : "会话 ID 是原始值") + maskNote(r) + cut + "；正文没脱敏，发出去前自己看一眼。";
  }
  function showBatchNote(text) {
    const n = $("#bx-note");
    n.textContent = text;
    n.classList.remove("hidden");
  }
  async function batchDownload(scope, btn) {
    return busy(btn, async () => {
      resetBatchNote();
      const r = await fetchBatch(scope);
      if (!r) return;
      saveText(r.filename, r.text, r.mime);
      showBatchNote("已生成 " + batchNote(r));
      toast("已导出；浏览器没弹下载的话点「复制本页」");
    });
  }
  $("#btn-bx-page").addEventListener("click", (e) => batchDownload("page", e.target));
  $("#btn-bx-search").addEventListener("click", (e) => batchDownload("search", e.target));
  $("#btn-bx-copy").addEventListener("click", (e) => busy(e.target, async () => {
    resetBatchNote();
    const r = await fetchBatch("page");
    if (!r) return;
    if (await copyText(r.text)) {
      showBatchNote("已复制 " + batchNote(r));
      toast("已复制到剪贴板");
    } else if (r.text.length > 1500000) {
      // 几 MB 的文字塞进文本框会卡住页面：不放，让用户改用下载
      showBatchNote("剪贴板不让写，而且这份文件太大（" + r.chars + " 字），放不进文本框。请改用「导出本页」下载。" + batchNote(r));
      toast("复制没成功：文件太大，请改用「导出本页」下载", true);
    } else {
      const fb = $("#bx-fallback");
      fb.value = r.text;
      fb.classList.remove("hidden");
      fb.select();
      showBatchNote("剪贴板不让写，下面文本框里是全文，全选复制即可。" + batchNote(r));
      toast("复制没成功，请从下面的文本框里全选复制", true);
    }
  }));
  function saveText(name, text, mime) {
    const url = URL.createObjectURL(new Blob([text], { type: mime || "text/plain" }));
    const a = el("a", { href: url, download: name });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
    } catch (e) { /* 没权限就走下面的老办法 */ }
    const ta = el("textarea", { style: "position:fixed;left:-9999px;top:0;opacity:0" });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    let done = false;
    try { done = document.execCommand("copy"); } catch (e) { done = false; }
    ta.remove();
    return done;
  }
  $("#btn-ex-download").addEventListener("click", (e) => busy(e.target, async () => {
    const r = await fetchExport();
    if (!r) return;
    saveText(r.filename, r.text, r.mime);
    $("#ex-note").textContent = "已生成 " + exportNote(r);
    toast("已导出；浏览器没弹下载的话点「复制」");
  }));
  $("#btn-ex-copy").addEventListener("click", (e) => busy(e.target, async () => {
    const r = await fetchExport();
    if (!r) return;
    const fb = $("#ex-fallback");
    if (await copyText(r.text)) {
      fb.classList.add("hidden");
      $("#ex-note").textContent = "已复制 " + exportNote(r);
      toast("已复制到剪贴板");
    } else {
      fb.value = r.text;
      fb.classList.remove("hidden");
      fb.select();
      $("#ex-note").textContent = "剪贴板不让写，下面文本框里是全文，全选复制即可。" + exportNote(r);
      toast("复制没成功，请从下面的文本框里全选复制", true);
    }
  }));

  // ------------------------------------------------------------ 单轮详情
  // terms：要在正文里高亮的检索词；hitSeqs：搜索命中的事件序号（打开后滚到第一处命中）
  function tokenText(run) {
    if (!(run.token_in || run.token_cached || run.token_out)) return "";
    const total = (run.token_in || 0) + (run.token_cached || 0);
    const hit = total > 0 && run.token_cached !== null && run.token_cached !== undefined
      ? `，命中缓存 ${run.token_cached}（${pct(run.token_cached / total)}）` : "";
    return ` · token 输入 ${total}${hit} / 输出 ${fmtNum(run.token_out)}`;
  }
  // 时间拆分：一条分段条 + 图例；各插件处理函数的耗时收在下面
  function renderTiming(t) {
    const box = clear($("#run-timing"));
    if (!t || !(t.segments || []).length) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    const total = t.total_s || 0;
    box.append(el("h4", {}, `时间拆分 · 合计 ${fmtSec(total)}`));
    const bar = el("div", { class: "tbar" });
    for (const s of t.segments) {
      bar.append(el("div", {
        class: "seg seg-" + s.key, style: `width:${total ? Math.max(0.5, (s.s / total) * 100) : 0}%`,
        title: `${s.label} ${fmtSec(s.s)}（${pct(total ? s.s / total : 0)}）\n${s.hint}`,
      }));
    }
    box.append(bar);
    box.append(el("div", { class: "tlegend" }, ...t.segments.map((s) => el("span", { class: "lg-" + s.key, title: s.hint },
      `${s.label} ${fmtSec(s.s)}`))));
    const calls = t.calls || [];
    if (calls.length) {
      const tb = el("table", { class: "table stat-table" },
        el("tr", {}, el("th", {}, "钩子"), el("th", {}, "插件 · 处理函数"), el("th", { class: "r" }, "次数"),
          el("th", { class: "r" }, "合计 ms"), el("th", { class: "r" }, "最慢 ms")),
        ...calls.slice(0, 30).map((c) => el("tr", {}, el("td", {}, c.hook_label), el("td", {}, `${c.plugin} · ${c.handler}`),
          el("td", { class: "r" }, c.n), el("td", { class: "r" }, c.total_ms), el("td", { class: "r" }, c.max_ms))));
      box.append(el("details", {}, el("summary", {}, `别的插件的处理函数耗时（${calls.length} 个，合计 ${t.hook_calls_total_ms} ms）`),
        el("div", { class: "table-wrap" }, tb)));
    }
    box.append(el("div", { class: "sub" }, (t.notes || []).join("；") + "。"));
  }
  // 上下文体积构成：条数 / 字数是量的，占比和 token 是按服务商总数折算的估算
  function renderContext(c) {
    const box = clear($("#run-context"));
    if (!c) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    box.append(el("h4", {}, "上下文体积构成（估算）"));
    if (c.error) { box.append(el("div", { class: "sub" }, "没数到：" + c.error)); return; }
    const head = [`历史 ${fmtNum(c.n_msgs)} 条，数到 ${c.total_chars} 个字符`];
    if (c.checkpoints) head.push(`另有 ${c.checkpoints} 条 checkpoint 内部标记没算`);
    if (c.tool_calls) head.push(`助手发起的工具调用 ${c.tool_calls} 次`);
    if (c.images) head.push(`历史里的图片 ${c.images} 张（token 没算）`);
    box.append(el("div", { class: "sub" }, head.join(" · ")));
    if (c.provider_total) {
      box.append(el("div", { class: "sub" }, `服务商报的输入合计 ${c.provider_total} token（没命中缓存 ${fmtNum(c.token_in)} + 命中缓存 ${fmtNum(c.token_cached)}）`
        + (c.cache_rate !== null && c.cache_rate !== undefined ? `，缓存命中率 ${pct(c.cache_rate)}` : "")));
    }
    const rows = c.rows || [];
    const tb = el("table", { class: "table stat-table" },
      el("tr", {}, el("th", {}, "部分"), el("th", { class: "r" }, "条数"), el("th", { class: "r" }, "中文等宽字符"),
        el("th", { class: "r" }, "其余字符"), el("th", {}, "约占（估算）"), el("th", { class: "r" }, "约 token（估算）")),
      ...rows.map((r) => el("tr", {},
        el("td", {}, r.label, r.tool_calls ? el("span", { class: "muted" }, ` · 含工具调用 ${r.tool_calls} 次`) : null),
        el("td", { class: "r" }, r.n), el("td", { class: "r" }, r.cjk), el("td", { class: "r" }, r.other),
        el("td", {}, r.share === undefined ? "—" : [el("span", { class: "sharebar", style: `width:${Math.round(r.share * 120)}px` }), pct(r.share)]),
        el("td", { class: "r" }, r.tokens === undefined ? "—" : r.tokens))));
    box.append(el("div", { class: "table-wrap" }, tb));
    if ((c.top || []).length) {
      box.append(el("details", {}, el("summary", {}, `历史里最长的 ${c.top.length} 条`),
        ...c.top.map((t) => el("div", { class: "sub" }, `第 ${t.i} 条 · ${t.role} · ${t.chars} 字 · 「${t.head}」`))));
    }
    box.append(el("div", { class: "sub" }, "说明：" + (c.note || "") + "。"));
  }
  async function openRun(node, runId, terms, hitSeqs) {
    terms = terms || [];
    document.querySelectorAll("#runs-list .run").forEach((n) => n.classList.remove("sel"));
    if (node) node.classList.add("sel");
    const r = await busy(null, () => api.get("api/run", { id: runId }));
    if (!r) return;
    const run = r.run;
    curRunId = runId;
    resetExportNote();
    resetSpDiff(runId);
    $("#run-detail-card").classList.remove("hidden");
    $("#run-title").textContent = (run.user_text || "（空提问）").slice(0, 60);
    clear($("#run-meta")).append(
      `状态 ${STATUS_TXT[run.status] || run.status} · 来源 ${run.source} · ${run.n_steps} 步 · ${run.n_tools} 工具 · ${fmtTime(run.sort_at)}`,
      run.model ? ` · 模型 ${run.model}` : "",
      tokenText(run),
      run.ttft ? ` · 首 token ${run.ttft}s` : "");
    renderTiming(r.timing);
    renderContext(r.context);
    const sp = clear($("#sysprompt-box"));
    if (r.sysprompt) {
      sp.append(el("div", { class: "meta" },
        `system prompt：${r.sysprompt.size === null ? "?" : r.sysprompt.size + " 字节"}`,
        r.sysprompt.same_as_prev ? "（和上一轮相同）" : "",
        " ",
        el("button", { class: "small", onclick: (e) => showSysprompt(e.target, r.sysprompt.hash) }, "查看全文")));
      sp.append(el("pre", { id: "sysprompt-text", class: "result hidden" }));
    }
    const box = clear($("#events"));
    const nodeOf = new Map();
    for (const e of r.events) { const n = eventNode(e, terms); nodeOf.set(e, n); box.append(n); }
    const inj = clear($("#run-injects"));
    const injs = r.events.filter((e) => e.kind === "inject");
    if (injs.length) {
      const groups = new Map();
      for (const e of injs) {
        const label = injectLabel(e);
        const g = groups.get(label) || { n: 0, len: 0, first: e, exact: (e.data || {}).attrib === "exact" };
        g.n += 1;
        g.len += (e.text || "").length;
        groups.set(label, g);
      }
      inj.append(el("span", { class: "muted" }, `本轮注入 ${injs.length} 项，来自：`),
        ...[...groups.entries()].map(([label, g]) => el("span", {
          class: "kchip k-inject" + (g.exact ? "" : " dim"), title: "点击跳到这个来源的第一项",
          onclick: () => { try { nodeOf.get(g.first).scrollIntoView({ block: "nearest" }); } catch (e2) { /* 跳不动就算了 */ } },
        }, `${label} · ${g.n} 项 · ${fmtLen(g.len)}`)));
    }
    const chg = r.events.filter((e) => e.kind === "edit" || e.kind === "sent");
    if (chg.length) {
      const groups = new Map();
      for (const e of chg) {
        const d = e.data || {};
        const labels = e.kind === "edit" ? [`${d.plugin} · ${e.name}`]
          : ((d.rewrites || []).length ? [...new Set(d.rewrites.map((w) => `${w.plugin} · 改写${w.hook === "on_decorating_result" ? "发出内容" : "回复"}`))]
            : ["发出内容有变（没抓到是谁）"]);
        for (const label of labels) {
          const g = groups.get(label) || { n: 0, first: e, kind: e.kind };
          g.n += 1;
          groups.set(label, g);
        }
      }
      inj.append(el("span", { class: "muted" }, `本轮改动 ${chg.length} 项：`),
        ...[...groups.entries()].map(([label, g]) => el("span", {
          class: "kchip k-" + g.kind, title: "点击跳到这一项",
          onclick: () => { try { nodeOf.get(g.first).scrollIntoView({ block: "nearest" }); } catch (e2) { /* 跳不动就算了 */ } },
        }, g.n > 1 ? `${label} ×${g.n}` : label)));
    }
    // 详情卡不在视口里才滚过去，免得点下面回合时整页乱跳
    try {
      const card = $("#run-detail-card");
      const rect = card.getBoundingClientRect();
      if (rect.top < 0 || rect.bottom > window.innerHeight) card.scrollIntoView({ block: "nearest" });
    } catch (e2) { /* 量一下位置而已，失败不滚 */ }
    // 从搜索结果点进来：滚到第一处命中并闪一下
    if (hitSeqs && hitSeqs.length) {
      const target = r.events.find((e) => hitSeqs.includes(e.seq));
      const tn = target && nodeOf.get(target);
      if (tn) {
        try { tn.scrollIntoView({ block: "center" }); } catch (e2) { /* 跳不动就算了 */ }
        tn.classList.add("flash");
        setTimeout(() => tn.classList.remove("flash"), 1800);
      }
    }
  }
  // ------------------------------------------------------------ system prompt 和上一轮对比（改动处红框，标谁改的）
  let spBrief = null;      // 概要（不带逐行），一打开这一轮就取
  let spFull = null;       // 带逐行结果，点“看改了哪里”才取
  let spFullFor = null;    // spFull 是哪一轮的
  let spMode = "full";     // full：完整提示词 + 红框；changes：只看改动处（前后各留两行）
  let spHunkEls = [];
  let spCur = -1;
  let spPos = null;
  const fmtClock = (t) => {
    try { return new Date(t * 1000).toLocaleTimeString("zh-CN", { hour12: false }); } catch (e) { return String(t); }
  };
  // 红框里只有空白时看不见，换成可见的符号
  const visWs = (t) => (t === "" ? "↵" : (t.trim() === "" ? t.replace(/ /g, "·").replace(/\t/g, "→") : t));
  function spChipText(s) {
    if (s.attrib === "earlier") return "更早就在 · 内核或更高优先级";
    if (s.attrib === "exact") return (s.side === "old" ? "上一轮的 " : "") + `${s.plugin} · ${s.handler}`;
    return (s.side === "old" ? "上一轮：" : "") + "没抓到是谁" + (s.guess && s.guess.length ? `（疑似 ${s.guess[0]}）` : "");
  }
  // 一处改动的来源标签：谁加的 / 改的
  function spWhoNodes(w, pureDelete) {
    if (w.attrib === "more") return [el("span", { class: "badge" }, `…还有 ${w.n} 个来源`)];
    if (w.attrib === "earlier") {
      return [el("span", { class: "badge", title: "没有被包装的处理函数加过这段：AstrBot 内核拼的、优先级比会话流还高的插件，或者人设 / 配置变了" },
        pureDelete ? "更早就在 · 内核或更高优先级（这一轮没了）" : "更早就在 · 内核或更高优先级")];
    }
    if (w.attrib === "exact") {
      const name = `${w.plugin} · ${w.handler}`;
      const txt = w.side === "old" ? (pureDelete ? `上一轮由 ${name} 加的，这一轮没了` : `上一轮这里是 ${name} 加的`) : `由 ${name} 加的`;
      return [el("span", { class: "badge who", title: `精确归属：${w.plugin} 的 ${w.handler} 在 on_llm_request 里加了这段` + (w.name ? `（注入块：${w.name}）` : "") }, txt)];
    }
    const out = [el("span", { class: "badge unk", title: "没被任何被包装的处理函数记到：可能是后注册的处理函数、被后来的处理函数改写，或宿主内部接口不可用" },
      (w.side === "old" ? "上一轮：" : "") + "没抓到是谁" + (w.name ? ` · ${w.name}` : ""))];
    if (w.guess && w.guess.length) out.push(el("span", { class: "badge guess", title: "在这些插件 / 内核源码里找到了同名标签，只是推测，不是定论" }, "疑似 " + w.guess.join(" / ")));
    return out;
  }
  function resetSpDiff(runId) {
    spBrief = null; spFull = null; spFullFor = null; spHunkEls = []; spCur = -1;
    clear($("#sp-diff")).classList.add("hidden");
    renderSpBlock(null);
    loadSpBrief(runId);
  }
  async function loadSpBrief(runId) {
    let d = null, msg = "";
    try { d = await api.get("api/sysprompt_diff", { id: runId }); } catch (e) { msg = e.message || String(e); }
    if (curRunId !== runId) return;      // 用户已经点开别的回合
    spBrief = d;
    renderSpBlock(d, msg);
  }
  // 详情右上角的小块：和上一轮比，改了几处、主要是谁
  function renderSpBlock(d, errMsg) {
    const box = clear($("#sp-block"));
    box.classList.remove("hidden");
    box.className = "sp-block";
    const when = d && d.prev && d.prev.sort_at ? `（${fmtClock(d.prev.sort_at)}）` : "";
    const head = (right) => el("div", { class: "sp-title" }, el("span", {}, "系统提示词 · 对比上一轮" + when), right || null);
    // 原生 append(null) 会写出“null”两个字，所以这里放数组、用的时候展开
    const skip = d && d.prev && d.prev.skipped ? [el("div", { class: "sp-text" }, `上一轮没存提示词，对比的是更早的一轮（中间跳过 ${d.prev.skipped} 轮）`)] : [];
    if (errMsg) { box.append(head(), el("div", { class: "sp-text" }, "对比没取到：" + errMsg)); return; }
    if (!d) { box.append(head(), el("div", { class: "sp-text" }, "对比中…")); return; }
    if (d.state === "no_prompt" || d.state === "no_prev") { box.append(head(), el("div", { class: "sp-text" }, d.text || "")); return; }
    if (d.state === "same") {
      box.classList.add("same");
      box.append(head(), el("div", { class: "sp-state" }, "和上一轮完全一样 ✓"), el("div", { class: "sp-text" }, `${d.new_size} 字节`), ...skip);
      return;
    }
    box.classList.add("chg");
    if (!d.n_hunks) {
      box.append(head(), el("div", { class: "sp-state" }, "有变化"), el("div", { class: "sp-text" }, d.note || "没逐行对比"), ...skip);
      return;
    }
    box.append(
      head(el("button", { class: "small", id: "btn-sp-diff", onclick: (e) => openSpDiff(e.target) }, "看改了哪里")),
      el("div", { class: "sp-state" }, el("b", {}, `改了 ${d.n_hunks} 处`), ` · 新增约 ${d.added} 字 / 去掉约 ${d.removed} 字`),
      el("div", { class: "sp-who" }, ...(d.who_summary || []).slice(0, 3).map((s) => el("span", {
        class: "kchip k-inject" + (s.attrib === "exact" ? "" : " dim"), title: "这个来源涉及的改动处数",
      }, spChipText(s) + (s.n > 1 ? ` ×${s.n}` : ""))),
      (d.who_summary || []).length > 3 ? el("span", { class: "muted" }, `…共 ${d.who_summary.length} 个来源`) : null),
      ...skip);
  }
  async function openSpDiff(btn) {
    const runId = curRunId;
    if (!spFull || spFullFor !== runId) {
      const d = await busy(btn, () => api.get("api/sysprompt_diff", { id: runId, lines: 1 }));
      if (!d || curRunId !== runId) return;
      spFull = d; spFullFor = runId;
    }
    spMode = "full";
    renderSpPanel();
    const panel = $("#sp-diff");
    try { panel.scrollIntoView({ block: "nearest" }); } catch (e) { /* 滚不动就算了 */ }
    jumpSp(0, true);
  }
  function spSetMode(m) {
    spMode = m;
    renderSpPanel();
    jumpSp(Math.max(spCur, 0), true);
  }
  function jumpSp(i, abs) {
    if (!spHunkEls.length) return;
    spCur = abs ? Math.min(Math.max(i, 0), spHunkEls.length - 1) : (spCur + i + spHunkEls.length) % spHunkEls.length;
    if (spPos) spPos.textContent = `第 ${spCur + 1} / ${spHunkEls.length} 处`;
    const target = spHunkEls[spCur];
    const body = $("#sp-diff .spd");
    if (body && target) {
      const top = target.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop - 24;
      body.scrollTo({ top: Math.max(0, top) });
      target.classList.add("flash");
      setTimeout(() => target.classList.remove("flash"), 1500);
    }
  }
  function spLine(no, kids, cls) {
    return el("div", { class: "spd-line" + (cls ? " " + cls : "") }, el("span", { class: "spd-no" }, no === null ? "" : String(no)), el("span", { class: "spd-t" }, kids));
  }
  function spItemNode(it) {
    if (it.k === "chg") {
      return spLine(it.n, it.segs.map(([kind, t]) => (kind === 0 ? t
        : el("span", { class: kind === 1 ? "spd-chg" : "spd-del", title: kind === 1 ? "这一轮新增 / 改成的" : "上一轮有、这一轮没了" }, visWs(t)))), "is-chg");
    }
    if (it.k === "add") return spLine(it.n, el("span", { class: "spd-chg", title: "这一轮新增的一行" }, visWs(it.t)), "is-add");
    return spLine("−", el("span", { class: "spd-del", title: "上一轮有、这一轮没了的一行" }, visWs(it.t)), "is-del");
  }
  function renderSpPanel() {
    const d = spFull;
    const box = clear($("#sp-diff"));
    box.classList.remove("hidden");
    spHunkEls = [];
    const bar = el("div", { class: "sp-bar inline" },
      el("b", {}, "系统提示词改动"),
      el("button", { class: "small" + (spMode === "full" ? " on" : ""), onclick: () => spSetMode("full") }, "完整提示词（改动处红框）"),
      el("button", { class: "small" + (spMode === "changes" ? " on" : ""), onclick: () => spSetMode("changes") }, "只看改动处"),
      el("button", { class: "small", onclick: () => jumpSp(-1) }, "◀ 上一处"),
      (spPos = el("span", { class: "muted" }, "")),
      el("button", { class: "small", onclick: () => jumpSp(1) }, "下一处 ▶"),
      el("button", { class: "small", onclick: () => { box.classList.add("hidden"); } }, "收起"));
    box.append(bar);
    const prevAt = d.prev && d.prev.sort_at ? fmtClock(d.prev.sort_at) : "";
    box.append(el("div", { class: "sp-legend" },
      el("span", {}, `对比上一轮（${prevAt}）：`),
      el("span", {}, el("span", { class: "spd-chg" }, "红框"), " 新增 / 改后"),
      el("span", {}, el("span", { class: "spd-del" }, "红虚线划掉"), " 上一轮有、这一轮没了"),
      el("span", { class: "muted" }, `${d.old_size} → ${d.new_size} 字节`)));
    const body = el("div", { class: "spd" });
    const items = d.lines || [];
    const hunkOf = (h) => (d.hunks || [])[h - 1] || {};
    let i = 0;
    while (i < items.length) {
      const it = items[i];
      if (it.k === "same") {
        const rows = it.t.split("\n");
        const first = i === 0, last = i === items.length - 1;
        if (spMode === "changes" && rows.length > 5) {
          const head = first ? [] : rows.slice(0, 2), tail = last ? [] : rows.slice(-2);
          const hidden = rows.length - head.length - tail.length;
          head.forEach((t, k) => body.append(spLine(it.n + k, t)));
          body.append(el("div", { class: "spd-skip" }, `… 中间 ${hidden} 行没变，已省略 …`));
          tail.forEach((t, k) => body.append(spLine(it.n + rows.length - tail.length + k, t)));
        } else {
          rows.forEach((t, k) => body.append(spLine(it.n + k, t)));
        }
        i += 1;
        continue;
      }
      const hid = it.h;
      const group = [];
      while (i < items.length && items[i].k !== "same" && items[i].h === hid) { group.push(items[i]); i += 1; }
      const hk = hunkOf(hid);
      const pureDelete = !hk.new_lines && hk.old_lines;
      const wrap = el("div", { class: "spd-hunk" });
      wrap.append(el("div", { class: "spd-head" },
        el("b", {}, `第 ${spHunkEls.length + 1} 处`),
        el("span", {}, `第 ${hk.n || group[0].n} 行`),
        hk.added || hk.removed ? el("span", {}, `+${hk.added || 0} / −${hk.removed || 0} 字`) : null,
        ...((hk.who || []).length ? (hk.who || []).flatMap((w) => spWhoNodes(w, pureDelete)) : [el("span", { class: "muted" }, "只是空行 / 分隔线有变")])));
      group.forEach((g) => wrap.append(spItemNode(g)));
      body.append(wrap);
      spHunkEls.push(wrap);
    }
    if (d.truncated) body.append(el("div", { class: "spd-skip" }, "改动处太多，后面的没有逐个标来源"));
    box.append(body);
    if (spPos) spPos.textContent = spHunkEls.length ? `第 ${Math.max(spCur, 0) + 1} / ${spHunkEls.length} 处` : "";
  }
  async function showSysprompt(btn, hash) {
    const r = await busy(btn, () => api.get("api/sysprompt", { hash }));
    if (!r) return;
    const pre = $("#sysprompt-text");
    pre.classList.remove("hidden");
    pre.textContent = r.text;
  }
  const KIND_TXT = { user: "用户", text: "回复", think: "思考", tool_call: "调工具", tool_result: "工具结果", note: "备注", error: "报错", inject: "注入", edit: "改动", sent: "实际发出" };
  const HOOK_TXT = { on_llm_request: "请求前", on_llm_response: "回复后", on_decorating_result: "发送前" };
  const SRC_TXT = { system_prompt: "系统提示词", prompt: "提问", extra_part: "用户消息附加块" };
  const LONG_AT = { user: 2000, text: 2000 };  // 其余种类超过 600 字先折叠，点按钮看全文
  const fmtLen = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + "k 字" : n + " 字");
  function kindChip(kind, name, isErr) {
    return el("span", { class: "kchip k-" + (isErr ? "error" : kind) }, (KIND_TXT[kind] || kind) + (name && name !== KIND_TXT[kind] ? " · " + name : ""));
  }
  // 长文本折叠：全文都在 DOM 里，只是先收起，点“展开全文”看全部
  // terms：检索词，正文里标出来；含检索词的长文本默认展开，免得命中被折叠在看不见的地方
  function hasTerm(text, terms) {
    const low = text.toLowerCase();
    return terms.some((t) => low.includes(t.toLowerCase()));
  }
  function bodyNode(text, limit, terms) {
    terms = terms || [];
    const body = el("div", { class: "body" }, highlight(text, terms));
    if (text.length <= limit && text.split("\n").length <= 16) return body;
    const open = terms.length > 0 && hasTerm(text, terms);
    if (!open) body.classList.add("clamp");
    const label = (collapsed) => (collapsed ? `展开全文（${fmtLen(text.length)}）` : "收起");
    const btn = el("button", { class: "small more" }, label(!open));
    btn.addEventListener("click", () => {
      btn.textContent = label(body.classList.toggle("clamp"));
    });
    return el("div", {}, body, btn);
  }
  // 谁塞的：精确（包装别的插件的处理函数拍快照）/ 更早就在 / 没抓到 / 源码里找到同名标签的推测
  function attribNodes(e) {
    const d = e.data || {};
    const out = [];
    if (d.attrib === "exact") {
      out.push(el("span", { class: "badge who", title: `精确归属：${d.plugin} 的 ${d.handler}（优先级 ${d.priority === undefined ? "?" : d.priority}）在 on_llm_request 里改了请求` }, `由 ${d.plugin} · ${d.handler}`));
      if (d.gone) out.push(el("span", { class: "badge unk", title: "这段内容后来又被别的处理函数改掉了，最终发给模型的已经不是原文" }, "后被改动"));
    } else if (d.attrib === "earlier") {
      out.push(el("span", { class: "badge", title: "会话流在最高优先级拍快照时它就已经在了：AstrBot 内核，或优先级比会话流还高的插件" }, "更早就在 · 内核或更高优先级"));
    } else if (d.attrib === "unattributed") {
      out.push(el("span", { class: "badge unk", title: "没被任何被包装的处理函数记到：可能是后注册的处理函数、被后来的处理函数改写，或宿主内部接口不可用" }, "没抓到是谁"));
    }
    if (d.guess && d.guess.length) {
      out.push(el("span", { class: "badge guess", title: "在这些插件 / 内核源码里找到了同名标签，只是推测，不是定论" }, "疑似 " + d.guess.join(" / ")));
    }
    return out;
  }
  // 实际发出：谁改写了回复（回复钩子 / 发送前钩子），或没抓到是谁
  function sentNodes(e) {
    const d = e.data || {};
    const rws = d.rewrites || [];
    if (!rws.length) {
      return [el("span", { class: "badge unk", title: "发出的内容和模型原话不一样，但没有被包装的处理函数记到改动：可能是还没包装的处理函数、宿主自己，或优先级更高的插件" }, "没抓到是谁改的")];
    }
    return rws.map((w) => el("span", { class: "badge who", title: `${w.plugin} 的 ${w.handler}（${HOOK_TXT[w.hook] || w.hook}钩子，优先级 ${w.priority === undefined ? "?" : w.priority}）改写了内容` },
      `由 ${w.plugin} · ${w.handler} 改写`));
  }
  function rewriteLines(e) {
    const clip = (t) => { const s = (t || "").replace(/\n+/g, " ↵ "); return s.length > 300 ? s.slice(0, 300) + "…" : s; };
    return ((e.data || {}).rewrites || []).map((w) => el("div", { class: "rewrite" },
      el("b", {}, `${w.plugin} · ${w.handler}（${HOOK_TXT[w.hook] || w.hook}）`), w.cleared ? " 清空了消息" : " 改写：",
      w.cleared ? "" : `「${clip(w.before)}」→「${clip(w.after)}」`));
  }
  function injectLabel(e) {
    const d = e.data || {};
    if (d.attrib === "exact") return d.plugin;
    if (d.attrib === "earlier") return "内核 / 更早";
    if (d.attrib === "unattributed") return "未归属";
    return d.guess && d.guess.length ? "疑似 " + d.guess[0] : "来源未知";
  }
  function eventNode(e, terms) {
    const k = e.is_error ? "error" : e.kind;
    const src = e.data && e.data.source;
    const kids = [el("div", { class: "kind" },
      kindChip(e.kind, e.name, e.is_error),
      src ? el("span", { class: "badge" }, "来自" + (SRC_TXT[src] || src)) : null,
      ...(e.kind === "inject" ? attribNodes(e) : []),
      ...(e.kind === "edit" ? attribNodes(e) : []),
      ...(e.kind === "sent" ? sentNodes(e) : []),
      e.t ? el("span", {}, fmtTime(e.t)) : null)];
    if (e.text) kids.push(bodyNode(e.text, LONG_AT[e.kind] || 600, terms));
    if (e.data && e.data.removed) kids.push(el("div", { class: "removed" }, "被换掉的原文：" + e.data.removed));
    if (e.kind === "sent") kids.push(...rewriteLines(e));
    if (e.data && (e.data.args || e.data.arguments)) {
      try { kids.push(el("pre", { class: "args" }, typeof e.data.args === "string" ? e.data.args : JSON.stringify(e.data.args || e.data.arguments, null, 2))); }
      catch (e2) { /* 展示失败就跳过参数 */ }
    }
    if (e.data && e.data.truncated) kids.push(el("div", { class: "kind" }, `（原文 ${e.data.truncated} 字，已截断）`));
    return el("div", { class: "ev k-" + k }, ...kids);
  }

  // ------------------------------------------------------------ 检索
  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#search-q").value.trim();
    if (!q) return;
    lastSearchQ = q;
    busy(e.submitter, async () => renderSearch(await api.get("api/search", { q, limit: 50 })));
  });
  // 把检索词里的自由词 / err: / user: 的内容在文本里标出来（只用文本节点和 <mark>，不拼 HTML）
  function searchTerms(q) {
    const out = [];
    for (const tok of (q || "").split(/\s+/)) {
      if (!tok) continue;
      const m = tok.match(/^(tool|umo):/i);
      if (m) continue;
      out.push(tok.replace(/^(err|user):/i, ""));
    }
    return out.filter((t) => t.length > 0);
  }
  function highlight(text, terms) {
    const t = text || "";
    if (!terms.length) return [t];
    const low = t.toLowerCase();
    const spans = [];
    for (const term of terms) {
      const lt = term.toLowerCase();
      let from = 0;
      for (;;) {
        const i = low.indexOf(lt, from);
        if (i < 0) break;
        spans.push([i, i + lt.length]);
        from = i + lt.length;
      }
    }
    if (spans.length > 600) return [t];   // 1 个字的检索词在大段文本里能命中上万处，不标了，免得页面卡
    spans.sort((a, b) => a[0] - b[0]);
    const out = [];
    let pos = 0;
    for (const [a, b] of spans) {
      if (b <= pos) continue;
      const s0 = Math.max(a, pos);
      if (s0 > pos) out.push(t.slice(pos, s0));
      out.push(el("mark", {}, t.slice(s0, b)));
      pos = b;
    }
    if (pos < t.length) out.push(t.slice(pos));
    return out;
  }
  let lastSearchQ = "";
  function renderSearch(r) {
    const meta = clear($("#search-meta"));
    meta.textContent = `${r.hits.length} 条命中` + (r.truncated ? "（只展示前一部分）" : "");
    const box = clear($("#search-results"));
    if (!r.hits.length) box.append(el("div", { class: "card muted" }, "没有命中，换个词试试。"));
    for (const h of r.hits) {
      box.append(el("div", { class: h.is_error ? "hit err" : "hit" },
        el("div", { class: "head" },
          kindChip(h.kind, h.name, h.is_error),
          el("span", { class: "muted" }, h.display_name || h.umo),
          el("span", { class: "muted" }, fmtTime(h.t)),
          el("button", { class: "small", onclick: () => openSearchRun(h.umo, h.run_id, searchTerms(lastSearchQ), [h.seq]) }, "看整轮")),
        el("div", { class: "snippet" }, highlight(h.snippet, searchTerms(lastSearchQ)))));
    }
  }
  async function openSearchRun(umo, runId, terms, hitSeqs) {
    showTab("sessions");
    await loadOverview();
    await openUmo(umo);
    openRun(null, runId, terms, hitSeqs);
  }

  // ------------------------------------------------------------ 旧档
  async function refreshLegacy() {
    const r = await busy(null, () => api.get("api/legacy/status"));
    if (r) renderLegacy(r);
  }
  function renderLegacy(p) {
    const lines = [`状态：${p.state}`, `进度：${p.done}/${p.total} 对话 · 导入 ${p.imported_runs} 轮 · 跳过 ${p.skipped}`];
    if (p.finished_at) lines.push("完成时间：" + fmtTime(p.finished_at));
    if ((p.errors || []).length) lines.push("错误：\n" + p.errors.join("\n"));
    $("#legacy-result").textContent = lines.join("\n");
  }
  $("#btn-legacy").addEventListener("click", (e) => busy(e.target, async () => {
    const r = await api.post("api/legacy/import", { force: $("#legacy-force").checked });
    toast(r.started ? "导入已开始，稍后点“刷新进度”看结果" : "没启动：" + JSON.stringify(r));
    setTimeout(refreshLegacy, 1500);
  }));
  $("#btn-legacy-status").addEventListener("click", (e) => busy(e.target, refreshLegacy));

  // ------------------------------------------------------------ 管理
  async function loadAdmin() {
    const r = await busy(null, () => api.get("api/admin/stats"));
    if (r) $("#admin-stats").textContent = JSON.stringify(r, null, 2);
  }
  // ---- 谁在往上下文里塞东西（按插件统计注入量）
  const SRC_SHORT = { system_prompt: "system prompt", prompt: "提问", extra_part: "附加块" };
  async function loadInjectStats() {
    const sel = $("#is-umo");
    const keep = sel.value;
    clear(sel).append(el("option", { value: "" }, "全部会话"),
      ...((overview && overview.umos) || []).map((u) => el("option", { value: u.umo }, u.display_name || u.umo)));
    sel.value = keep;
    const params = { days: $("#is-days").value };
    if (sel.value) params.umo = sel.value;
    const r = await busy($("#btn-is-refresh"), () => api.get("api/stats/inject", params));
    if (r) renderInjectStats(r);
  }
  function renderInjectStats(r) {
    $("#is-summary").textContent = `${r.days ? "最近 " + r.days + " 天" : "全部时间"}内 ${r.live_runs} 个实时回合，其中 ${r.runs_with_inject} 轮有注入，` +
      `共 ${r.inject_n} 项 / ${r.inject_chars} 字。`;
    const t = clear($("#is-table"));
    if (!r.rows.length) { t.append(el("tr", {}, el("td", { class: "muted" }, "这段时间没有记到注入。"))); }
    else {
      t.append(el("tr", {}, el("th", {}, "来源"), el("th", { class: "r" }, "轮数"), el("th", { class: "r" }, "项数"),
        el("th", { class: "r" }, "总字数"), el("th", { class: "r" }, "每次出现平均"), el("th", { class: "r" }, "出现率"),
        el("th", {}, "占比"), el("th", {}, "位置")));
      for (const x of r.rows) {
        t.append(el("tr", {},
          el("td", {}, x.label, x.kind === "guess" ? el("span", { class: "badge guess", title: "只是推测" }, "推测") : null,
            x.kind === "unattributed" ? el("span", { class: "badge unk" }, "没抓到") : null,
            el("div", { class: "muted" }, x.names.map(([n, c]) => `${n}×${c}`).join("、"))),
          el("td", { class: "r" }, x.runs), el("td", { class: "r" }, x.n), el("td", { class: "r" }, x.chars),
          el("td", { class: "r" }, x.avg_per_run + " 字"), el("td", { class: "r" }, pct(x.presence)),
          el("td", {}, el("span", { class: "sharebar", style: `width:${Math.round(x.share * 100)}px` }), pct(x.share)),
          el("td", { class: "muted" }, Object.entries(x.by_source).map(([k, v]) => `${SRC_SHORT[k] || k} ${v}`).join(" · "))));
      }
    }
    const ed = clear($("#is-edits"));
    if ((r.edits || []).length) {
      ed.append(el("div", {}, "改动（谁改了请求里的历史 / 工具集 / 图片 / 模型）："),
        ...r.edits.slice(0, 12).map((e) => el("div", {}, `${e.plugin} 改了${e.what} ${e.n} 次（${e.runs} 轮）` +
          (e.what === "历史" && e.removed ? `，一共删去 ${e.removed} 条` : ""))));
    }
  }
  $("#btn-is-refresh").addEventListener("click", () => loadInjectStats());
  $("#is-days").addEventListener("change", () => loadInjectStats());
  $("#is-umo").addEventListener("change", () => loadInjectStats());

  // ---- 导出屏蔽规则（存在插件库里；这里是编辑用的草稿，点保存才生效）
  let maskDraft = null;
  let maskDirty = false;
  const KIND_NAME = Object.fromEntries(MASK_KINDS);
  function renderMaskEditor() {
    if (maskDraft === null) maskDraft = (maskInfo.rules || []).map((r) => ({ ...r }));
    const sel = $("#mk-kind");
    if (!sel.options.length) {
      sel.append(...MASK_KINDS.map(([k, txt]) => el("option", { value: k }, txt)));
      sel.addEventListener("change", syncMaskName);
    }
    syncMaskName();
    const list = clear($("#mask-list"));
    maskDraft.forEach((r, i) => {
      const cb = el("input", { type: "checkbox", title: "关掉 = 规则留着但暂时不生效" });
      cb.checked = r.on !== false;
      cb.addEventListener("change", () => { r.on = cb.checked; maskDirty = true; renderMaskEditor(); });
      list.append(el("div", { class: "mask-row" + (r.on === false ? " off" : "") },
        el("label", { class: "check" }, cb, " 生效"),
        el("span", { class: "mr-kind" }, KIND_NAME[r.kind] || r.kind),
        el("span", { class: "mr-name" }, r.name ? r.name : "（这一种全部）"),
        el("button", { class: "small danger", onclick: () => { maskDraft.splice(i, 1); maskDirty = true; renderMaskEditor(); } }, "删除")));
    });
    $("#mk-note").textContent = maskDirty ? "有改动还没保存，点「保存规则」才会生效。" : "";
  }
  function syncMaskName() {
    const kind = $("#mk-kind").value;
    const nameless = ["system_prompt", "user", "text", "think"].includes(kind);
    const inp = $("#mk-name");
    inp.disabled = nameless;
    if (nameless) inp.value = "";
    inp.placeholder = nameless ? "这一种没有名字，只能整种屏蔽" : "名字 / 来源插件（可留空 = 这一种全部，支持 *）";
    const dl = clear($("#mk-names"));
    for (const x of ((maskInfo.seen || {})[kind] || []).slice(0, 80)) dl.append(el("option", { value: x.name }, `${x.name}（见过 ${x.n} 次）`));
  }
  $("#btn-mk-add").addEventListener("click", () => {
    if (maskDraft === null) maskDraft = [];
    const kind = $("#mk-kind").value;
    const name = $("#mk-name").value.trim();
    if (maskDraft.some((r) => r.kind === kind && (r.name || "").toLowerCase() === name.toLowerCase())) { toast("这条规则已经有了", true); return; }
    maskDraft.push({ kind, name, on: true });
    maskDirty = true;
    $("#mk-name").value = "";
    renderMaskEditor();
  });
  $("#btn-mk-save").addEventListener("click", (e) => busy(e.target, async () => {
    const r = await api.post("api/mask_rules/save", { rules: maskDraft || [] });
    maskInfo.rules = r.rules;
    maskDraft = r.rules.map((x) => ({ ...x }));
    maskDirty = false;
    buildMaskBoxes();
    renderMaskEditor();
    toast(`已保存 ${r.rules.length} 条屏蔽规则`);
  }));

  // ---- 运行诊断：查看 / 下载（不含聊天正文，后端已保证）
  async function fetchDiag(lines) {
    return api.get("api/diag", { raw: $("#diag-raw").checked ? "1" : "0", lines });
  }
  function diagSummary(b) {
    const hooks = Object.entries(b.hooks || {}).map(([k, h]) =>
      `  ${k}: ${h.count} 次，报错 ${h.errors}，平均 ${h.avg_ms}ms，最慢 ${h.max_ms}ms`).join("\n") || "  （还没有钩子触发）";
    const tasks = ((b.runtime || {}).tasks || []).map((t) =>
      `  ${t.name}: ${t.done ? "已结束 " + (t.error || "") : "运行中"}`).join("\n");
    const db = b.db || {};
    const errs = (b.recent_errors || []).slice(-5).map((e) => `  ${e.at} ${e.message}`).join("\n") || "  （没有）";
    return [
      `生成时间 ${b.generated_at} · 插件 ${(b.env || {}).plugin_version || "?"} · AstrBot ${(b.env || {}).astrbot_version || "?"} · 已运行 ${b.uptime_s}s`,
      `库：${db.runs} 轮 / ${db.events} 事件，${db.db_bytes} 字节，索引 ${(db.indexes || []).length} 个`,
      `写库队列：${(b.runtime || {}).write_queue}` + writeSummary((b.runtime || {}).write), `后台任务：\n${tasks}`,
      attribSummary((b.runtime || {}).attribution),
      `钩子统计：\n${hooks}`, `最近报错（共 ${b.error_count} 次）：\n${errs}`,
      "", "—— 日志尾巴 ——", ...(b.log_tail || []).slice(-60),
    ].join("\n");
  }
  function writeSummary(w) {
    if (!w) return "";
    return `；写库成功 ${w.done} 次，失败 ${w.failed} 次` + (w.last_fail ? `（最近一次：${w.last_fail}）` : "");
  }
  function attribSummary(a) {
    if (!a) return "注入归属：（这个版本没有）";
    const st = a.status || {};
    const head = st.available === false ? `注入归属：不可用（${st.error || "宿主内部接口不匹配"}），只剩“更早就在 / 疑似”`
      : st.available ? `注入归属：可用，已包装 ${st.wrapped} 个处理函数（${Object.entries(st.by_hook || {}).map(([k, n]) => `${HOOK_TXT[k] || k.replace(/^on_/, "")} ${n}`).join("、") || "—"}）` : "注入归属：还没有请求经过，尚未包装";
    const rows = Object.entries(a.handlers || {}).sort((x, y) => y[1].max_ms - x[1].max_ms).slice(0, 8)
      .map(([k, h]) => `  ${k}（${HOOK_TXT[h.hook] || (h.hook || "?").replace(/^on_/, "")}）: ${h.count} 次，平均 ${h.avg_ms}ms，最慢 ${h.max_ms}ms`).join("\n");
    return head + (rows ? "\n各钩子上的处理函数（按最慢排，别的插件的）：\n" + rows : "");
  }
  $("#btn-diag-refresh").addEventListener("click", (e) => busy(e.target, async () => {
    const b = await fetchDiag(300);
    $("#diag-view").textContent = diagSummary(b);
    $("#diag-file").textContent = "日志文件：" + ((b.log_file || {}).path || "（没写文件）") + "（滚动保留最近约 4MB）";
  }));
  $("#btn-diag-download").addEventListener("click", (e) => busy(e.target, async () => {
    const b = await fetchDiag(1500);
    const stamp = (b.generated_at || "").replace(/[-: ]/g, "").replace(/^(\d{8})(\d{6}).*$/, "$1-$2");
    const blob = new Blob([JSON.stringify(b, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: `session_flow_diag_${stamp || "now"}.json` });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    $("#diag-view").textContent = diagSummary(b);
    toast("诊断包已生成；浏览器没弹下载的话，把下面这段文字全选复制发过去也行（比完整包少一些）");
  }));

  // 清理是不可逆删除，插件页 iframe 禁弹窗（没有 allow-modals，confirm 直接返回 false），
  // 所以用页内两步确认：第一次点按钮变红字“再点一次确认删除”，4 秒内再点才执行。
  let purgeArmedAt = 0;
  let purgeTimer = null;
  function disarmPurge(btn, text) {
    purgeArmedAt = 0;
    clearTimeout(purgeTimer);
    purgeTimer = null;
    if (btn) { btn.textContent = text; btn.classList.remove("armed"); }
  }
  $("#btn-purge").addEventListener("click", (e) => {
    const btn = e.target;
    const days = Number($("#purge-days").value);
    if (!(days > 0)) { toast("天数必须大于 0", true); return; }
    const now = Date.now();
    if (!purgeArmedAt || now - purgeArmedAt > 4000) {
      purgeArmedAt = now;
      btn.textContent = `再点一次确认删除（${days} 天前）`;
      btn.classList.add("armed");
      clearTimeout(purgeTimer);
      purgeTimer = setTimeout(() => disarmPurge(btn, "删除"), 4000);
      return;
    }
    disarmPurge(btn, "删除");
    busy(btn, async () => {
      const r = await api.post("api/admin/purge", { days });
      toast(`删掉了 ${r.deleted_runs} 轮`);
      await loadAdmin();
      loaded.sessions = false;
    });
  });

  // ------------------------------------------------------------ 顶部横幅（只在真出问题时亮）
  function renderHealth(problems) {
    const box = clear($("#health-banner"));
    box.classList.toggle("hidden", !problems.length);
    for (const p of problems) {
      box.append(el("div", { class: "health-item " + (p.level === "warn" ? "warn" : "error") },
        el("b", {}, "⚠ " + p.title), p.text,
        el("button", { class: "small health-go", onclick: () => { showTab("admin"); } }, "去看诊断")));
    }
  }
  async function loadHealth() {
    try {
      const r = await api.get("api/health");
      renderHealth(r.problems || []);
    } catch (e) { /* 自检接口取不到不另外报，免得横幅自己成了噪音 */ }
  }
  setInterval(() => { if (!document.hidden) loadHealth(); }, 30000);

  // ------------------------------------------------------------ 启动
  // 运行中 3 秒轮询：只在会话页可见时刷 running 标记
  setInterval(() => {
    if (document.hidden) return;
    if (!document.getElementById("tab-sessions").classList.contains("active")) return;
    loadOverviewQuiet();
  }, 3000);
  async function start() {
    if (!bridge) {
      const f = $("#fatal");
      f.textContent = "没有检测到 AstrBot 插件页桥接（window.AstrBotPluginPage）。请在 AstrBot WebUI 的插件页面里打开这个页面。";
      f.classList.remove("hidden");
      return;
    }
    try {
      await bridge.ready();
      loadHealth();
      loadMaskRules();
      await loadOverview();
      const [tab, arg] = decodeURIComponent(location.hash.slice(1)).split(/:(.*)/s);
      if (tab && document.getElementById("tab-" + tab)) showTab(tab);
      if (tab === "search" && arg) {
        $("#search-q").value = arg;
        lastSearchQ = arg;
        renderSearch(await api.get("api/search", { q: arg, limit: 50 }));
      }
    } catch (e) {
      const f = $("#fatal");
      f.textContent = "加载失败：" + (e.message || e) + "\n（插件是否已启用？后端接口是否注册成功？可以看 AstrBot 日志里的 [session_flow]）";
      f.classList.remove("hidden");
    }
  }
  start();
})();
