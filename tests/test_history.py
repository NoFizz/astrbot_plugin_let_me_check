"""Tests for history.py: atomic write path and umo-preserving fallback."""

import asyncio
import builtins
from types import SimpleNamespace

from astrbot_plugin_let_me_check.history import HistoryManager


class FakeConv:
    def __init__(self):
        self.history = []


class FakeCM:
    def __init__(self):
        self.calls = []
        self.curr_cid = "cid-1"

    async def get_curr_conversation_id(self, umo):
        return self.curr_cid

    async def add_message_pair(self, cid, user_message, assistant_message):
        self.calls.append(("pair", cid, user_message, assistant_message))

    async def get_conversation(self, umo, cid, create_if_not_exists=False):
        self.calls.append(("get_conv", umo, cid))
        return FakeConv()

    async def update_conversation(self, umo, cid, history=None, **kwargs):
        self.calls.append(("update", umo, cid, history))


def make_hm():
    cm = FakeCM()
    ctx = SimpleNamespace(conversation_manager=cm)
    return HistoryManager(ctx), cm


def test_atomic_pair_path_writes_both_parts():
    hm, cm = make_hm()
    asyncio.run(hm.write_forward_pair("umo-1", "user text", "assistant text"))
    assert cm.calls[0][0] == "pair"
    assert cm.calls[0][1] == "cid-1"
    user_msg = cm.calls[0][2]
    assistant_msg = cm.calls[0][3]
    assert user_msg.content[0].text == "user text"
    assert assistant_msg.content[0].text == "assistant text"


def test_empty_assistant_uses_update_path():
    hm, cm = make_hm()
    asyncio.run(hm.write_forward_pair("umo-1", "user text", ""))
    methods = [c[0] for c in cm.calls]
    assert "update" in methods
    assert "pair" not in methods


def test_import_error_fallback_keeps_caller_umo(monkeypatch):
    """Regression: the ImportError fallback must pass the caller's umo
    through instead of reading a private attribute from the manager."""
    hm, cm = make_hm()
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "astrbot.core.agent.message":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    asyncio.run(hm.write_forward_pair("umo-1", "user text", "assistant text"))

    updates = [c for c in cm.calls if c[0] == "update"]
    assert updates, f"expected update_conversation fallback, calls={cm.calls}"
    assert updates[0][1] == "umo-1"


def test_missing_conversation_manager_is_silent():
    hm = HistoryManager(SimpleNamespace())  # no conversation_manager attr
    asyncio.run(hm.write_forward_pair("umo-1", "user text", "assistant text"))  # no raise


def test_curr_cid_missing_is_silent():
    cm = FakeCM()
    cm.curr_cid = None
    ctx = SimpleNamespace(conversation_manager=cm)
    hm = HistoryManager(ctx)
    asyncio.run(hm.write_forward_pair("umo-1", "user text", "assistant text"))
    assert cm.calls == []
