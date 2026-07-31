"""Tests for history.py: atomic write path and umo-preserving fallback."""

import asyncio
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

    async def get_conversation(self, umo, cid, create_if_not_exists=False):
        self.calls.append(("get_conv", umo, cid))
        return FakeConv()

    async def update_conversation(self, umo, cid, history=None, **kwargs):
        self.calls.append(("update", umo, cid, history))


def make_hm():
    cm = FakeCM()
    ctx = SimpleNamespace(conversation_manager=cm)
    return HistoryManager(ctx), cm


def test_empty_assistant_uses_update_path():
    hm, cm = make_hm()
    asyncio.run(hm.write_forward_pair("umo-1", "user text", ""))
    methods = [c[0] for c in cm.calls]
    assert "update" in methods
    assert "pair" not in methods


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
