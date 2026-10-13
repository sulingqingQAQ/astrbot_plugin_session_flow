# -*- coding: utf-8 -*-
"""离线测试总入口：python -I tests\run_all.py（用测试端自带 Python）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

# 沙箱 python 的系统临时目录 sqlite 写不进：测试临时文件改落工作区内
_TMP = HERE / ".tmp"
_TMP.mkdir(exist_ok=True)
tempfile.tempdir = str(_TMP)

import _stubs  # noqa: E402

_stubs.install_astrbot_stub()

MODULES = (
    "test_store",
    "test_extract",
    "test_recorder",
    "test_legacy",
    "test_search",
    "test_pager",
    "test_hooks_ext",
    "test_export",
    "test_v018",
    "test_spdiff",
    "test_inject_diff",
    "test_attrib",
    "test_summary",
    "test_webapi",
    "test_diag",
    "test_main_wiring",
    "test_page_static",
)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in MODULES:
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except ModuleNotFoundError as e:
            print(f"[run_all] 跳过 {name}：{e}")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print(f"用例 {result.testsRun} 个，通过 {result.testsRun - len(result.failures) - len(result.errors)} 个，"
          f"失败 {len(result.failures)} 个，出错 {len(result.errors)} 个")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
