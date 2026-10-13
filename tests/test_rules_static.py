# -*- coding: utf-8 -*-
"""插件市场的日志规范：logger 只能从 astrbot.api 导入，不许用 Python 内置 logging 模块。

main.py 和 sf/ 下的代码一律不许 import logging、不许 getLogger；
要记日志就走 main.py 传进来的宿主 logger（经 DiagLog 转发），或 DiagLog 自己。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _plugin_sources() -> list[Path]:
    files = [ROOT / "main.py", ROOT / "__init__.py"]
    files += sorted((ROOT / "sf").glob("*.py"))
    return [p for p in files if p.exists()]


class TestHostLoggerRules(unittest.TestCase):
    def test_sources_found(self):
        names = {p.name for p in _plugin_sources()}
        self.assertIn("main.py", names)
        self.assertIn("diag.py", names)
        self.assertIn("webapi.py", names)

    def test_no_stdlib_logging(self):
        bad = []
        for p in _plugin_sources():
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    bad += [f"{p.name}:{node.lineno} import {a.name}" for a in node.names
                            if a.name == "logging" or a.name.startswith("logging.")]
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if node.level == 0 and (mod == "logging" or mod.startswith("logging.")):
                        bad.append(f"{p.name}:{node.lineno} from {mod} import …")
                elif isinstance(node, ast.Attribute) and node.attr in ("getLogger", "RotatingFileHandler"):
                    bad.append(f"{p.name}:{node.lineno} .{node.attr}")
        self.assertEqual(bad, [], "插件代码里不许用内置 logging：" + "; ".join(bad))

    def test_main_uses_astrbot_api_logger(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        ok = any(isinstance(n, ast.ImportFrom) and n.module == "astrbot.api"
                 and any(a.name == "logger" for a in n.names) for n in ast.walk(tree))
        self.assertTrue(ok, "main.py 必须 from astrbot.api import logger")


if __name__ == "__main__":
    unittest.main()
