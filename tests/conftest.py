"""Pytest bootstrap: stub the ``astrbot`` package tree so the plugin modules
can be imported standalone without bootstrapping the full AstrBot framework.

The stubs mirror the real component shapes (``Forward``/``Nodes`` segments,
``TextPart.mark_as_temp()`` semantics, ``AiocqhttpMessageEvent`` interface)
that the plugin actually relies on.
"""

import sys
import types
from pathlib import Path

import pytest

PLUGINS_DIR = Path(__file__).resolve().parents[2]  # data/plugins/


def _namespace_pkg(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = []
    sys.modules[name] = mod
    return mod


def _leaf_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


# ── astrbot namespace packages ─────────────────────────────────
for _pkg in (
    "astrbot",
    "astrbot.api",
    "astrbot.core",
    "astrbot.core.provider",
    "astrbot.core.agent",
    "astrbot.core.platform",
    "astrbot.core.platform.sources",
    "astrbot.core.platform.sources.aiocqhttp",
):
    _namespace_pkg(_pkg)

# ── astrbot.api.logger (collector) ─────────────────────────────
_log_records: list[tuple[str, str]] = []


def _log_collector(level: str):
    """构造收集型 logger stub：记录 (level, message) 供测试断言。"""

    def _collect(*args, **kwargs):
        _log_records.append((level, str(args[0]) if args else ""))
        return None

    return _collect


_logger_mod = _leaf_module(
    "astrbot.api.logger",
    debug=_log_collector("debug"),
    info=_log_collector("info"),
    warning=_log_collector("warning"),
    error=_log_collector("error"),
    critical=_log_collector("critical"),
    exception=_log_collector("exception"),
)
_logger_mod.logs = _log_records


@pytest.fixture(autouse=True)
def _reset_logs():
    """每个测试前后清空收集的日志记录，保证用例隔离。"""
    _log_records.clear()
    yield
    _log_records.clear()

# ── astrbot.api.message_components ─────────────────────────────
class _BaseComponent:
    type: str = "unknown"

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class Forward(_BaseComponent):
    """Mirror of Comp.Forward: carries id/resid or inline data payload."""

    type = "forward"


class Nodes(_BaseComponent):
    """Mirror of Comp.Nodes: carries a nodes list."""

    type = "nodes"


class Text(_BaseComponent):
    """Mirror of Comp.Text: carries a text payload."""

    type = "text"


_leaf_module("astrbot.api.message_components", Forward=Forward, Nodes=Nodes, Text=Text)

# ── astrbot.api.event ──────────────────────────────────────────
class EventMessageType:
    ALL = "all"


class AstrMessageEvent:
    def __init__(self, message_obj=None, platform="aiocqhttp"):
        self.message_obj = message_obj
        self.platform = platform


class _Filter:
    EventMessageType = EventMessageType

    def on_llm_request(self):
        def deco(fn):
            return fn

        return deco

    def event_message_type(self, *args, **kwargs):
        def deco(fn):
            return fn

        return deco


_leaf_module(
    "astrbot.api.event",
    AstrMessageEvent=AstrMessageEvent,
    filter=_Filter(),
    EventMessageType=EventMessageType,
)

# ── astrbot.api.star ───────────────────────────────────────────
class Context:
    pass


class Star:
    def __init__(self, context=None):
        self.context = context


def register(name, author, desc, version, repo_url):
    def deco(cls):
        return cls

    return deco


_leaf_module("astrbot.api.star", Context=Context, Star=Star, register=register)

# ── astrbot.api.provider ───────────────────────────────────────
class ProviderRequest:
    def __init__(self):
        self.extra_user_content_parts = []


_leaf_module("astrbot.api.provider", ProviderRequest=ProviderRequest)

# ── astrbot.api (AstrBotConfig) ────────────────────────────────
class AstrBotConfig(dict):
    pass


_api_pkg = sys.modules["astrbot.api"]
_api_pkg.AstrBotConfig = AstrBotConfig
_api_pkg.logger = sys.modules["astrbot.api.logger"]

# ── astrbot.core.provider.provider ─────────────────────────────
class Provider:
    async def text_chat(self, prompt="", image_urls=None):
        raise NotImplementedError


_leaf_module("astrbot.core.provider.provider", Provider=Provider)

# ── astrbot.core.agent.message ─────────────────────────────────
class TextPart:
    """Mirror of the real TextPart: mark_as_temp() sets _no_save."""

    def __init__(self, text=""):
        self.text = text
        self._no_save = False

    def mark_as_temp(self):
        self._no_save = True
        return self


class ImageURLPart:
    """Mirror of the real ImageURLPart: carries image url, mark_as_temp() sets _no_save."""

    class ImageURL:
        def __init__(self, url="", id=None):
            self.url = url
            self.id = id

    def __init__(self, image_url=None):
        self.image_url = image_url or self.ImageURL()
        self._no_save = False

    def mark_as_temp(self):
        self._no_save = True
        return self


class UserMessageSegment:
    def __init__(self, content=None):
        self.content = content or []


class AssistantMessageSegment:
    def __init__(self, content=None):
        self.content = content or []


_leaf_module(
    "astrbot.core.agent.message",
    TextPart=TextPart,
    ImageURLPart=ImageURLPart,
    UserMessageSegment=UserMessageSegment,
    AssistantMessageSegment=AssistantMessageSegment,
)

# ── astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event ──
class AiocqhttpMessageEvent:
    """Minimal stand-in for the real aiocqhttp event used by plugin tests."""

    def __init__(
        self,
        message_obj=None,
        bot=None,
        unified_msg_origin="aiocqhttp:GroupMessage:123456",
        group_id="123456",
        sender_name="测试用户",
    ):
        self.message_obj = message_obj
        self.bot = bot
        self.unified_msg_origin = unified_msg_origin
        self._group_id = group_id
        self._sender_name = sender_name
        self._stopped = False

    def get_group_id(self):
        return self._group_id

    def get_sender_name(self):
        return self._sender_name

    def stop_event(self):
        self._stopped = True


_leaf_module(
    "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
    AiocqhttpMessageEvent=AiocqhttpMessageEvent,
)

# ── plugin import path ─────────────────────────────────────────
sys.path.insert(0, str(PLUGINS_DIR))


# ── shared fakes ───────────────────────────────────────────────
class FakeMessageObj:
    """Message object with .message (segment list) and .raw_message."""

    def __init__(self, segments=None, raw_message=None):
        self.message = segments or []
        self.raw_message = raw_message


class FakeBot:
    """Protocol bot: call_action returns a canned get_forward_msg payload."""

    def __init__(self, forward_payload=None):
        self.forward_payload = forward_payload
        self.calls = []
        self.api = self  # parser calls event.bot.api.call_action()

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        return self.forward_payload


@pytest.fixture
def make_event():
    """Factory for AiocqhttpMessageEvent with FakeMessageObj + FakeBot."""

    def _make(
        segments=None,
        raw_message=None,
        bot_payload=None,
        umo="aiocqhttp:GroupMessage:123456",
        group_id="123456",
        sender_name="测试用户",
    ):
        msg_obj = FakeMessageObj(segments=segments, raw_message=raw_message)
        bot = FakeBot(forward_payload=bot_payload)
        return AiocqhttpMessageEvent(
            message_obj=msg_obj,
            bot=bot,
            unified_msg_origin=umo,
            group_id=group_id,
            sender_name=sender_name,
        )

    return _make
