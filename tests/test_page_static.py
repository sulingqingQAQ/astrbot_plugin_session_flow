# -*- coding: utf-8 -*-
"""页面静态检查（没浏览器点的部分，用文本锁住）：S5 + 暗色 + 接口/id 对账。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PAGES = Path(__file__).resolve().parent.parent / "pages" / "session_flow"


def read(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


class TestPageStatic(unittest.TestCase):
    def test_no_modal_dialogs(self):
        # S5：iframe 没 allow-modals，confirm/alert/prompt 全废，只能页内确认
        js = read("app.js")
        js = re.sub(r"showSysprompt", "show_XX", js)  # 函数名里带 prompt，不算
        for fn in ("confirm", "alert", "prompt"):
            self.assertNotRegex(js, r"(?<![\w$])" + fn + r"\s*\(")

    def test_dark_theme(self):
        css = read("app.css")
        self.assertIn('[data-theme="dark"]', css)
        self.assertIn("prefers-color-scheme", css)

    def test_endpoints_exist(self):
        from sf import webapi as webapi_mod
        js = read("app.js")
        routes = {r for r, _, _ in webapi_mod.SessionFlowWebApi(None, None).routes()}
        used = set(re.findall(r'api\.(?:get|post)\("([^"]+)"', js))
        self.assertTrue(used)
        for ep in used:
            self.assertIn(ep, routes, f"页面调了不存在的接口 {ep}")

    def test_pager_and_search_controls(self):
        # v0.1.4：分页条 + 搜本对话的控件都在；“加载更多”已经没有了
        from sf import search as search_mod
        html = read("index.html")
        for i in ("runs-pager", "pg-size", "pg-prev", "pg-next", "pg-jump", "pg-pages", "pg-total",
                  "runs-search", "rs-q", "rs-kind", "rs-clear", "rs-go", "runs-list"):
            self.assertIn(f'id="{i}"', html, i)
        self.assertNotIn("btn-more", html)
        self.assertNotIn("btn-more", read("app.js"))
        # 内容类型下拉的取值要和后端认的一致，写错了会悄悄变成“不筛选”
        sel = html.split('id="rs-kind"')[1].split("</select>")[0]
        opts = set(re.findall(r'<option value="(\w*)"', sel)) - {""}
        self.assertEqual(opts, set(search_mod.KIND_GROUPS) | {search_mod.KIND_ERROR})
        # 每页条数的选项在页面里写死 20/50/100，JS 里认的是同一组
        sizes = set(re.findall(r'<option value="(\d+)">\d+ 条/页', html))
        self.assertEqual(sizes, {"20", "50", "100"})
        self.assertIn("const SIZES = [20, 50, 100]", read("app.js"))

    def test_localstorage_is_guarded(self):
        # 无痕窗口 / 屏蔽站点数据时 localStorage 会抛，用到的地方都要在 try 里
        js = read("app.js")
        uses = [m.start() for m in re.finditer(r"localStorage", js)]
        self.assertTrue(uses)
        for pos in uses:
            self.assertIn("try", js[max(0, pos - 160):pos])

    def test_export_controls(self):
        # v0.1.6：单轮详情里有导出控件，调的是 api/export
        html = read("index.html")
        js = read("app.js")
        for i in ("ex-fmt", "btn-ex-download", "btn-ex-copy", "ex-raw", "ex-note", "ex-fallback"):
            self.assertIn(f'id="{i}"', html)
        self.assertIn('"api/export"', js)
        self.assertIn("execCommand", js)                 # 剪贴板不让写时的退路
        self.assertNotIn("confirm(", js)

    def test_v018_controls(self):
        # v0.1.8：顶部横幅 / 时间拆分和体积构成面板 / 批量导出 / 屏蔽规则 / 注入统计，页面和后端接口对得上
        html = read("index.html")
        js = read("app.js")
        for i in ("health-banner", "run-timing", "run-context", "batch-export", "bx-fmt", "btn-bx-page", "btn-bx-search",
                  "btn-bx-copy", "bx-raw", "bx-mask-box", "ex-mask-box", "bx-note", "bx-fallback",
                  "inject-stats-card", "is-days", "is-umo", "is-table", "is-summary", "is-edits", "btn-is-refresh",
                  "mask-card", "mask-list", "mk-kind", "mk-name", "mk-names", "btn-mk-add", "btn-mk-save", "mk-note"):
            self.assertIn(f'id="{i}"', html, i)
        for ep in ("api/export_many", "api/mask_rules", "api/mask_rules/save", "api/stats/inject", "api/health"):
            self.assertIn(f'"{ep}"', js, ep)
        # 屏蔽种类在页面里写死一份，必须和后端认的一致，写错了会悄悄变成“没屏蔽”
        from sf import mask as mask_mod
        kinds = set(re.findall(r'\["(\w+)", "[^"]+"\]', js.split("const MASK_KINDS = ")[1].split(";")[0]))
        self.assertEqual(kinds, set(mask_mod.KINDS))

    def test_v019_controls(self):
        # v0.1.9：详情右上角的“system prompt 对比上一轮”小块 + 红框视图，页面和后端接口对得上
        html = read("index.html")
        js = read("app.js")
        css = read("app.css")
        for i in ("sp-block", "sp-diff", "run-title", "run-meta"):
            self.assertIn(f'id="{i}"', html, i)
        self.assertIn('"api/sysprompt_diff"', js)
        # 小块和标题 / 元信息在同一个头部里（右上角）
        head = html.split('class="run-head"')[1].split("<div class=\"inline export-bar\"")[0]
        for i in ("run-title", "run-meta", "sp-block"):
            self.assertIn(f'id="{i}"', head, i)
        # 红框：新增 / 改后是实线，上一轮有、这一轮没了是虚线划掉；样式用主题里的 --danger，亮暗两套都跟着变
        self.assertRegex(css, r"\.spd-chg\s*\{[^}]*border:\s*2px solid var\(--danger\)")
        self.assertRegex(css, r"\.spd-del\s*\{[^}]*border:\s*2px dashed var\(--danger\)")
        self.assertIn("line-through", css)
        # 原生 append(null) 会把“null”两个字写进页面：渲染小块的函数里不能出现把可能为 null 的变量直接传给 append 的写法
        block = js.split("function renderSpBlock")[1].split("async function openSpDiff")[0]
        self.assertNotRegex(block, r"box\.append\([^;]*,\s*skip\s*\)")

    def test_element_ids(self):
        js = read("app.js")
        html = read("index.html")
        defined = set(re.findall(r'id="([\w-]+)"', html)) | set(re.findall(r'id:\s*"([\w-]+)"', js))
        used = set(re.findall(r'\$\("#([\w-]+)"\)', js))
        for i in sorted(used):
            self.assertIn(i, defined, f"页面用了不存在的元素 #{i}")


if __name__ == "__main__":
    unittest.main()
