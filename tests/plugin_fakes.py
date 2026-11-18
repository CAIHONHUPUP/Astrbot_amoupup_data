# -*- coding: utf-8 -*-
"""Phase 18：本插件测试里重复定义的 Fake* 统一收口到这里（只搬位置，断言不变）。

三插件的 tests/support 保持逐字节一致，本文件放插件特有 / 历史测试专用桩。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import os
import random
import sqlite3
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class MemStatsFakeMem:
    async def stats(self, bot):
        assert bot == ""
        return {"memories": 7, "memories_active": 5, "profiles": 2,
                "persons": 3, "messages": 9, "relations": 1}
