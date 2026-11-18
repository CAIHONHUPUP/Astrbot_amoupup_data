# -*- coding: utf-8 -*-
"""测试辅助包：只放测试专用夹具，生产代码不 import 这里。

Phase 18：三个人格插件的 tests/support/{__init__,fakes}.py 逐字节一致。
"""
from .fakes import *  # noqa: F401,F403
from .fakes import __all__ as _FAKES_ALL
from .fakes import SUPPORT_VERSION  # noqa: F401

__all__ = list(_FAKES_ALL) + ["SUPPORT_VERSION"]
