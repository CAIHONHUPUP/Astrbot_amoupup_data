# -*- coding: utf-8 -*-
"""兼容壳：老测试 import clock。"""
import utils.clock as _c

globals().update({k: v for k, v in vars(_c).items() if not k.startswith("__")})
