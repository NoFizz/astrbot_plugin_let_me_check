"""Tests for main.py: on-demand staging, pure-forward swallow, trigger injection."""

import asyncio
import time
from types import SimpleNamespace

from astrbot.api import logger
from astrbot_plugin_let_me_check.main import SmartForward
from astrbot_plugin_let_me_check.parser import detect_forward

from astrbot.api.message_components import Forward, Text
from astrbot.api.provider import ProviderRequest


def make_plugin():
    config = {
        "group_chat": {"enable": True, "whitelist_enable": False, "whitelist": []},
        "private_chat": {"enable": True, "whitelist_enable": False, "whitelist": []},
        "max_messages": 200,
        "parse_nested_forward": True,
        "max_nested_depth": 3,
        "model_config": {
            "image_caption_concurrency": 5,
            "image_caption_prompt": "",
            "image_caption_enabled": True,
        },
    }
    return SmartForward(SimpleNamespace(), config)


def _fwd_payload(text="你好"):
    return {
        "messages": [
            {
                "sender": {"nickname": "甲"},
                "content": [{"type": "text", "data": {"text": text}}],
            }
        ]
    }


def test_non_aiocqhttp_platform_skipped():
    plugin = make_plugin()

    async def scenario():
        await plugin.on_message(object())  # not an AiocqhttpMessageEvent
        assert plugin._pending == {}

    asyncio.run(scenario())


def test_disabled_chat_skipped(make_event):
    plugin = make_plugin()
    plugin.group_chat = {"enable": False, "whitelist_enable": False, "whitelist": []}

    async def scenario():
        event = make_event(segments=[Forward(id="fwd-1")])
        await plugin.on_message(event)
        assert plugin._pending == {}

    asyncio.run(scenario())


def test_on_message_stages_without_parsing(make_event):
    """收到转发仅暂存，不解析（无后台任务、无结果缓存）。"""
    plugin = make_plugin()

    async def scenario():
        event = make_event(segments=[Forward(id="fwd-1")])
        await plugin.on_message(event)
        queue = plugin._pending.get(event.unified_msg_origin)
        assert queue is not None
        assert len(queue) == 1
        staged_event, detect_result, ts = queue[0]
        assert detect_result.forward_id == "fwd-1"
        assert staged_event is event
        assert ts <= time.monotonic()

    asyncio.run(scenario())


def test_dedup_skips_same_forward(make_event):
    plugin = make_plugin()

    async def scenario():
        event = make_event(segments=[Forward(id="fwd-x")])
        await plugin.on_message(event)
        await plugin.on_message(event)
        assert len(plugin._pending[event.unified_msg_origin]) == 1

    asyncio.run(scenario())


def test_pending_cap_drops_oldest(make_event):
    """超过 10 条时按 FIFO 淘汰最旧条目。"""
    plugin = make_plugin()

    async def scenario():
        umo = "aiocqhttp:GroupMessage:123456"
        for i in range(12):
            event = make_event(segments=[Forward(id=f"fwd-{i}")], umo=umo)
            await plugin.on_message(event)
        queue = plugin._pending[umo]
        assert len(queue) == 10
        assert queue[0][1].forward_id == "fwd-2"
        assert queue[-1][1].forward_id == "fwd-11"

    asyncio.run(scenario())


def test_pending_ttl_expired_cleaned(make_event):
    """超过 24h 的暂存条目在惰性清理时被移除。"""
    plugin = make_plugin()

    async def scenario():
        umo = "aiocqhttp:GroupMessage:123456"
        event = make_event(segments=[Forward(id="fwd-old")], umo=umo)
        await plugin.on_message(event)
        # 手动伪造过期时间戳（90000s > 86400s TTL）
        plugin._pending[umo][0] = (event, plugin._pending[umo][0][1], time.monotonic() - 90000)
        plugin._cleanup_expired_pending(umo)
        assert umo not in plugin._pending

    asyncio.run(scenario())


def test_pure_forward_swallowed(make_event):
    """纯转发消息：on_llm_request 调 stop_event 吞掉本次 LLM 调用。"""
    plugin = make_plugin()

    async def scenario():
        event = make_event(segments=[Forward(id="fwd-1")])
        req = ProviderRequest()
        await plugin.on_llm_request(event, req)
        assert event._stopped is True
        assert req.extra_user_content_parts == []

    asyncio.run(scenario())


def test_trigger_injects_merged_pending(make_event):
    """含实质内容的消息触发：取出全部暂存并合并注入（mark_as_temp 生效）。"""
    plugin = make_plugin()

    async def scenario():
        umo = "aiocqhttp:GroupMessage:123456"
        fwd1 = make_event(
            segments=[Forward(id="fwd-1")], umo=umo, bot_payload=_fwd_payload("转发内容")
        )
        await plugin.on_message(fwd1)
        trigger = make_event(segments=[Text(text="看看")], umo=umo)
        req = ProviderRequest()
        await plugin.on_llm_request(trigger, req)

        assert len(req.extra_user_content_parts) == 1
        part = req.extra_user_content_parts[0]
        assert part._no_save is True  # mark_as_temp() applied
        assert "<forwarded_message_context>" in part.text
        assert "转发内容" in part.text
        assert umo not in plugin._pending  # 注入后清空暂存

    asyncio.run(scenario())


def test_multiple_forwards_merged_in_order(make_event):
    """多条暂存转发按入队顺序合并注入。"""
    plugin = make_plugin()

    async def scenario():
        umo = "aiocqhttp:GroupMessage:123456"
        f1 = make_event(
            segments=[Forward(id="fwd-1")], umo=umo, bot_payload=_fwd_payload("第一条")
        )
        f2 = make_event(
            segments=[Forward(id="fwd-2")], umo=umo, bot_payload=_fwd_payload("第二条")
        )
        await plugin.on_message(f1)
        await plugin.on_message(f2)
        trigger = make_event(segments=[Text(text="总结")], umo=umo)
        req = ProviderRequest()
        await plugin.on_llm_request(trigger, req)
        text = req.extra_user_content_parts[0].text
        assert text.index("第一条") < text.index("第二条")

    asyncio.run(scenario())


def test_disabled_chat_pure_forward_not_swallowed(make_event):
    """私聊禁用时：纯转发在 on_llm_request 中不被吞掉（插件不生效）。"""
    plugin = make_plugin()
    plugin.private_chat = {"enable": False, "whitelist_enable": False, "whitelist": []}

    async def scenario():
        event = make_event(
            segments=[Forward(id="fwd-1")],
            umo="aiocqhttp:PrivateMessage:123456",
            group_id=None,
        )
        req = ProviderRequest()
        await plugin.on_llm_request(event, req)
        assert event._stopped is False
        assert plugin._pending == {}

    asyncio.run(scenario())


def test_trigger_without_pending_no_injection(make_event):
    """无暂存时触发：不注入、不吞调用（主管道正常处理用户消息）。"""
    plugin = make_plugin()

    async def scenario():
        trigger = make_event(segments=[Text(text="普通消息")])
        req = ProviderRequest()
        await plugin.on_llm_request(trigger, req)
        assert req.extra_user_content_parts == []
        assert trigger._stopped is False

    asyncio.run(scenario())


def test_process_pending_handles_cancelled_error_result(make_event):
    """gather(return_exceptions=True) 可能返回 CancelledError（BaseException 子类），
    必须以 BaseException 判断，避免访问 result.messages 时抛 AttributeError。"""
    plugin = make_plugin()

    class _CancelledBot:
        def __init__(self):
            self.api = self

        async def call_action(self, action, **params):
            raise asyncio.CancelledError()

    async def scenario():
        umo = "aiocqhttp:GroupMessage:123456"
        event = make_event(segments=[Forward(id="fwd-cancel")], umo=umo)
        event.bot = _CancelledBot()
        detect_result = detect_forward(event)
        assert detect_result is not None
        result = await plugin._process_pending(
            umo, [(event, detect_result, time.monotonic())]
        )
        assert result == (None, [])

    asyncio.run(scenario())


def test_process_pending_empty_returns_none():
    """空暂存列表：_process_pending 直接返回 (None, [])（不抛 IndexError）。"""
    plugin = make_plugin()

    async def scenario():
        result = await plugin._process_pending("umo", [])
        assert result == (None, [])

    asyncio.run(scenario())


def test_on_llm_request_logs_chat_model(make_event):
    """注入转发内容时日志记录将由哪个对话模型解析文字。"""
    plugin = make_plugin()

    class _ChatProvider:
        model_name = "main-chat-model"
        provider_config = {"id": "chat-cfg"}

    plugin.context.get_using_provider = lambda umo=None: _ChatProvider()

    async def scenario():
        umo = "aiocqhttp:GroupMessage:123456"
        fwd = make_event(
            segments=[Forward(id="fwd-1")], umo=umo, bot_payload=_fwd_payload("转发内容")
        )
        await plugin.on_message(fwd)
        trigger = make_event(segments=[Text(text="看看")], umo=umo)
        req = ProviderRequest()
        await plugin.on_llm_request(trigger, req)
        infos = [msg for lvl, msg in logger.logs if lvl == "info"]
        assert any(
            "对话模型" in msg and "chat-cfg / main-chat-model" in msg
            for msg in infos
        )

    asyncio.run(scenario())


def test_direct_image_injection_when_chat_model_multimodal(make_event):
    """当前对话模型支持图片输入（level 3）：图片以 ImageURLPart 原生注入，
    不调用图片转述模型。"""
    from astrbot.core.agent.message import ImageURLPart

    plugin = make_plugin()

    from astrbot.core.provider.provider import Provider as _StubProvider

    class _ChatProvider(_StubProvider):
        model_name = "mimo-v2.5"
        provider_config = {"id": "chat-mimo", "modalities": ["text", "image"]}

    # 补齐真实 AstrBot Context 的方法（make_plugin 用空 SimpleNamespace）
    plugin.context.get_using_provider = lambda umo=None: _ChatProvider()
    plugin.context.get_provider_by_id = lambda pid: None
    plugin.context.get_config = lambda umo=None: {
        "provider_settings": {},
        "provider_ltm_settings": {},
    }

    # 给 LLMService 注入可记录的 provider，验证 describe_images 未被调用
    calls = []
    plugin._llm.describe_images = lambda *a, **k: calls.append(a) or ["(图片)"]

    async def scenario():
        umo = "aiocqhttp:GroupMessage:123456"
        fwd = make_event(
            segments=[Forward(id="fwd-img")],
            umo=umo,
            bot_payload=_img_fwd_payload("https://example.com/a.jpg"),
        )
        await plugin.on_message(fwd)
        trigger = make_event(segments=[Text(text="看看")], umo=umo)
        req = ProviderRequest()
        await plugin.on_llm_request(trigger, req)

        # 转述未被调用
        assert calls == []
        # 注入包含 ImageURLPart 且 mark_as_temp
        parts = req.extra_user_content_parts
        img_parts = [p for p in parts if isinstance(p, ImageURLPart)]
        assert len(img_parts) == 1
        assert img_parts[0].image_url.url == "https://example.com/a.jpg"
        assert img_parts[0]._no_save is True

    asyncio.run(scenario())


def _img_fwd_payload(img_url):
    """构造含一张图片的转发负载。"""
    return {
        "messages": [
            {
                "sender": {"nickname": "甲"},
                "content": [
                    {"type": "image", "data": {"url": img_url}},
                    {"type": "text", "data": {"text": "看图"}},
                ],
            }
        ]
    }
