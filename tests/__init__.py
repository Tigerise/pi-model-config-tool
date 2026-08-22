# -*- coding: utf-8 -*-
"""让 tests 目录下的用例能直接 import 项目根目录里的模块。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
