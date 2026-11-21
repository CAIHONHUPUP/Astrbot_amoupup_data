# -*- coding: utf-8 -*-
"""最小 astrbot 桩：让 segment_engine / heartflow_engine 能在纯单测里 import。

两个引擎在模块顶层 `from astrbot.api...`，CI 上没有 AstrBot，之前这两个模块
（以及分段死循环、0 当默认值这类 bug）完全没有测试。这里只桩住 import 需要的东西，
不模拟平台行为。
"""

import sys
import types

#: 三插件共用的 canonical stub 版本（test_test_infra 会断言三份 sha256/版本一致）。
STUB_VERSION = "1.0"

#: install() 之后必须出现在 sys.modules / 模块属性里的名字（防回退）。
REQUIRED_MODULES = (
    "astrbot", "astrbot.api", "astrbot.api.star", "astrbot.api.event",
    "astrbot.api.message_components", "astrbot.api.provider",
    "astrbot.core", "astrbot.core.message", "astrbot.core.message.message_event_result",
    "astrbot.core.platform", "astrbot.core.platform.astr_message_event",
    "astrbot.core.platform.message_session",
)

_INSTALLED = False


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _build_api_module():
    """astrbot.api：logger + AstrBotConfig。"""
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()

    class AstrBotConfig(dict):
        def save_config(self):
            return True

    api.AstrBotConfig = AstrBotConfig
    return api


def _build_star_module():
    """astrbot.api.star：Star / Context。"""
    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None, config=None):
            self.context = context
            self.config = config or {}

    class Context:
        pass

    star.Star = Star
    star.Context = Context
    return star


def _build_event_module():
    """astrbot.api.event：AstrMessageEvent + filter 装饰器桩。"""
    event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    event.AstrMessageEvent = AstrMessageEvent

    class _Filter:
        class EventMessageType:
            ALL = "all"
            PRIVATE_MESSAGE = "private"
            GROUP_MESSAGE = "group"

        class PermissionType:
            ADMIN = "admin"
            MEMBER = "member"

        def __getattr__(self, name):
            def decorator(*args, **kwargs):
                def wrap(func):
                    return func
                return wrap
            return decorator

        def command(self, *args, **kwargs):
            return self.__getattr__("command")(*args, **kwargs)

    event.filter = _Filter()
    event.AstrBotEvent = AstrMessageEvent
    return event


def _build_components_module():
    """astrbot.api.message_components；返回 (模块, Plain 类)。"""
    components = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Reply:
        def __init__(self, id=None):
            self.id = id

    class At:
        def __init__(self, qq=""):
            self.qq = qq

    class Record:
        def __init__(self, file=None, url=None, text=""):
            self.file = file
            self.url = url or file
            self.text = text

    class Image:
        def __init__(self, file=None, **kwargs):
            self.file = file
            self.path = kwargs.get("path")

        @classmethod
        def fromFileSystem(cls, path, **kwargs):
            return cls(file=str(path), path=str(path))

        @classmethod
        def fromBase64(cls, payload, **kwargs):
            return cls(file="base64://" + str(payload))

        @classmethod
        def fromURL(cls, url, **kwargs):
            return cls(file=str(url))

    components.Plain = Plain
    components.Reply = Reply
    components.At = At
    components.Record = Record
    components.Image = Image
    return components, Plain


def _build_provider_module():
    """astrbot.api.provider：LLMResponse。"""
    provider = types.ModuleType("astrbot.api.provider")

    class LLMResponse:
        pass

    provider.LLMResponse = LLMResponse
    return provider


def _build_result_module(plain_cls):
    """astrbot.core.message.message_event_result：MessageChain。"""
    result_mod = types.ModuleType("astrbot.core.message.message_event_result")

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

        def clear(self):
            self.chain.clear()

        def append(self, item):
            self.chain.append(item)

        def message(self, text):
            self.chain.append(plain_cls(text))

    result_mod.MessageChain = MessageChain
    return result_mod


def _build_platform_modules():
    """(astr_message_event, message_session)：两个模块共用同一个 MessageSession。"""
    ame = types.ModuleType("astrbot.core.platform.astr_message_event")

    class MessageSession:
        pass

    ame.MessageSession = MessageSession

    session_mod = types.ModuleType("astrbot.core.platform.message_session")
    session_mod.MessageSession = MessageSession
    return ame, session_mod


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot.__dsh_stub__ = True
    api = _build_api_module()
    star = _build_star_module()
    event = _build_event_module()
    components, plain_cls = _build_components_module()
    provider = _build_provider_module()

    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    message_pkg = types.ModuleType("astrbot.core.message")
    message_pkg.__path__ = []
    result_mod = _build_result_module(plain_cls)
    platform_pkg = types.ModuleType("astrbot.core.platform")
    platform_pkg.__path__ = []
    ame, session_mod = _build_platform_modules()

    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": star,
        "astrbot.api.event": event,
        "astrbot.api.message_components": components,
        "astrbot.api.provider": provider,
        "astrbot.core": core,
        "astrbot.core.message": message_pkg,
        "astrbot.core.message.message_event_result": result_mod,
        "astrbot.core.platform": platform_pkg,
        "astrbot.core.platform.astr_message_event": ame,
        "astrbot.core.platform.message_session": session_mod,
    })


def is_installed():
    return bool(_INSTALLED)
