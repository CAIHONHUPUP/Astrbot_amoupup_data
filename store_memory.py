# -*- coding: utf-8 -*-
"""兼容壳：老测试 import store_memory（取 _is_meta_query / _split_long_text / 换 logger）。"""
import sys
import types

import services.memory_engine as _mem
from services.compression_engine import _split_long_text  # noqa: F401
from services.retrieval_engine import _is_meta_query  # noqa: F401

logger = _mem.logger


class _Shim(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "logger":
            _mem.logger = value


sys.modules[__name__].__class__ = _Shim
