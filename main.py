# -*- coding: utf-8 -*-
"""pi 模型配置工具入口。

带 --selftest 参数时跑无界面自检（就是 tests 目录里那套单元测试），
退出码 0 表示全过；带 --version 打印版本号。
"""
import io
import os
import sys


def _setup_stdio():
    """打包成无窗口程序后 stdout/stderr 可能是 None，做好保护。"""
    for name in ("stdout", "stderr"):
        s = getattr(sys, name)
        if s is not None and hasattr(s, "buffer"):
            setattr(sys, name, io.TextIOWrapper(
                s.buffer, encoding="utf-8", errors="replace"))


_setup_stdio()


def selftest(verbosity=2):
    """跑 tests 目录里的全部用例。打包后的 exe 也能跑。"""
    import unittest
    here = os.path.dirname(os.path.abspath(__file__))
    start = os.path.join(getattr(sys, "_MEIPASS", here), "tests")
    if not os.path.isdir(start):
        print("找不到 tests 目录（%s），跳过自检" % start)
        return 1
    sys.path.insert(0, os.path.dirname(start))
    suite = unittest.defaultTestLoader.discover(start_dir=start,
                                                top_level_dir=os.path.dirname(start))
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    if result.wasSuccessful():
        print("全部自检通过")
        return 0
    return 1


if __name__ == "__main__":
    if "--version" in sys.argv:
        import version
        print(version.title())
        sys.exit(0)
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    import ui
    sys.exit(ui.run())
