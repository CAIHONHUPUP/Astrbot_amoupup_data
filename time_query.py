# -*- coding: utf-8 -*-
"""兼容壳：老测试 import time_query。"""
import utils.time_query as _tq

globals().update({k: v for k, v in vars(_tq).items() if not k.startswith("__")})
