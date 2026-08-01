"""Tests for history.py: atomic write path and umo-preserving fallback."""

import asyncio
from types import SimpleNamespace

from astrbot_plugin_let_me_check.history import HistoryManager


class FakeConv:
    def __init__(self, history=None):
        self.history = history if history is not None else []


class FakeCM:
    def __init__(self):
        self.calls = []
        self.curr_cid = "cid-1"
        self.stored = {}  # (umo, cid) -> 已持久化的历史列表
        self.get_delay = 0.0  # get_conversation 的模拟延迟，用于制造并发交错

    async def get_curr_conversation_id(self, umo):
        return self.curr_cid

    async def get_conversation(self, umo, cid, create_if_not_exists=False):
        self.calls.append(("get_conv", umo, cid))
        # 读取快照（独立副本）后再延迟：两个并发任务都会先读到同一份空历史，
        # 模拟真实框架中两次独立读取的竞态窗口。
        history = list(self.stored.get((umo, cid), []))
        if self.get_delay:
            await asyncio.sleep(self.get_delay)
        return FakeConv(history)

    async def update_conversation(self, umo, cid, history=None, **kwargs):
        self.calls.append(("update", umo, cid, history))
        self.stored[(umo, cid)] = history


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


def test_concurrent_writes_do_not_lose_history():
    """并发写入同一会话时，两对消息都必须持久化，不能相互覆盖。"""
    cm = FakeCM()
    cm.get_delay = 0.05  # 强制两次 get_conversation 交错执行
    ctx = SimpleNamespace(conversation_manager=cm)
    hm = HistoryManager(ctx)

    async def scenario():
        await asyncio.gather(
            hm.write_forward_pair("umo-1", "pair-A", ""),
            hm.write_forward_pair("umo-1", "pair-B", ""),
        )

    asyncio.run(scenario())
    contents = [m["content"] for m in cm.stored[("umo-1", "cid-1")]]
    assert contents == ["pair-A", "pair-B"]
