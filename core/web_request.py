# -*- coding: utf-8 -*-
"""core/web_request.py —— 请求对象代理。

真实的 web_request 由 AstrBot/Quart 提供；测试会 patch main.web_request，
所以这里统一从 main 模块动态取，取不到再退回原始对象。
"""

try:
    from astrbot.api.web import request as _raw
except Exception:  # pragma: no cover
    try:
        from quart import request as _raw  # type: ignore
    except Exception:
        _raw = None


class _RequestProxy:
    def __init__(self, fallback):
        self._fallback = fallback

    def _target(self):
        import sys
        for name in ("main", "Astrbot_amoupup_data.main"):
            module = sys.modules.get(name)
            if module is not None and "web_request" in vars(module):
                return vars(module)["web_request"]
        return self._fallback

    def __getattr__(self, name):
        target = self._target()
        if target is None:
            raise AttributeError(name)
        return getattr(target, name)

    def __bool__(self):
        return self._target() is not None


web_request = _RequestProxy(_raw)
