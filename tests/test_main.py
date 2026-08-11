"""Tests for main.py: on-demand staging, pure-forward swallow, trigger injection."""

import asyncio
import time
from types import SimpleNamespace

from astrbot.api import logger
from astrbot_plugin_let_me_check.main import let_me_check
from astrbot_plugin_let_me_check.parser import detect_forward

from astrbot.api.message_components import Forward, Text
from astrbot.api.provider import ProviderRequest


def make_plugin():
    config = {
        "group_chat": {"enable": True, "whitelist_enable": False, "whitelist": []},
        "private_chat": {"enable": True, "whitelist_enable": False, "whitelist": []},
        "max_messages": 200,
        "parse_nested_forward": True,
        "max_nested_depth": 5,
        "model_config": {
            "image_caption_concurrency": 5,
            "image_caption_prompt": "",
            "image_caption_enabled": True,
        },
    }
    return let_me_check(SimpleNamespace(), config)


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
    """含实质内容的消息触发：取出全部暂存并合并注入（文字 part 不标 temp，
    交由 AstrBot 随本轮历史持久化，即"会话记忆"契约）。"""
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
        assert part._no_save is False  # 文字 part 必须被 AstrBot 保存（P0-01 契约）
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
        assert result == (None, [], None, 0)

    asyncio.run(scenario())


def test_process_pending_empty_returns_none():
    """空暂存列表：_process_pending 直接返回 (None, [], None, 0)（不抛 IndexError）。"""
    plugin = make_plugin()

    async def scenario():
        result = await plugin._process_pending("umo", [])
        assert result == (None, [], None, 0)

    asyncio.run(scenario())


def test_on_llm_request_logs_chat_model(make_event):
    """注入转发内容时日志记录解析模型全名。"""
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
            "已注入转发上下文" in msg and "chat-cfg" in msg
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


def test_cross_pending_share_fetch_semaphore(make_event):
    """多条暂存转发并行解析时共享同一协议拉取信号量：
    嵌套拉取峰值并发不超过 _NESTED_FETCH_CONCURRENCY（而非 条数 × 上限）。"""
    import astrbot_plugin_let_me_check.main as main_mod

    class CountingBot:
        """跟踪最大并发 call_action 调用数的协议端 FakeBot。"""

        def __init__(self):
            self.api = self
            self.active = 0
            self.max_active = 0

        async def call_action(self, action, **params):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.02)
                nid = str(params.get("id", ""))
                return {
                    "messages": [
                        {
                            "sender": {"nickname": f"N{nid}"},
                            "content": [_text_seg(f"nested-{nid}")],
                        }
                    ]
                }
            finally:
                self.active -= 1

    def _nested_payload(i):
        # 每条转发内联 4 个嵌套引用 → 顶层不走协议拉取，全部调用均为嵌套拉取
        return {
            "messages": [
                {
                    "sender": {"nickname": f"U{i}"},
                    "content": [
                        {"type": "text", "data": {"text": f"m{i}"}},
                        {"type": "forward", "data": {"id": f"n-{i}-0"}},
                        {"type": "forward", "data": {"id": f"n-{i}-1"}},
                        {"type": "forward", "data": {"id": f"n-{i}-2"}},
                        {"type": "forward", "data": {"id": f"n-{i}-3"}},
                    ],
                }
            ]
        }

    async def scenario():
        original = getattr(main_mod, "_NESTED_FETCH_CONCURRENCY", None)
        main_mod._NESTED_FETCH_CONCURRENCY = 2
        try:
            umo = "aiocqhttp:GroupMessage:123456"
            plugin = make_plugin()
            bots = []
            for i in range(4):  # 4 条 pending × 4 嵌套 = 16 次嵌套拉取
                bot = CountingBot()
                bots.append(bot)
                event = make_event(
                    segments=[Forward(data=_nested_payload(i))], umo=umo
                )
                event.bot = bot
                await plugin.on_message(event)
            trigger = make_event(segments=[Text(text="总结")], umo=umo)
            req = ProviderRequest()
            await plugin.on_llm_request(trigger, req)
            # 共享信号量将嵌套拉取总并发压到 2，而不是 4 条 × 单树上限
            assert max(b.max_active for b in bots) <= 2
            assert umo not in plugin._pending
        finally:
            if original is None:
                del main_mod._NESTED_FETCH_CONCURRENCY
            else:
                main_mod._NESTED_FETCH_CONCURRENCY = original

    asyncio.run(scenario())


def test_invalid_config_values_fall_back(make_event):
    """非法配置类型/范围在 _load_config 中被安全转换或回退默认值（不抛异常）。"""
    config = {
        "group_chat": {"enable": True, "whitelist_enable": False, "whitelist": []},
        "private_chat": {"enable": True, "whitelist_enable": False, "whitelist": []},
        "max_messages": "200",  # 字符串 → 转换
        "parse_nested_forward": "false",  # 字符串 bool → 回退 True
        "max_nested_depth": -3,  # 负数 → 裁剪到下限
        "model_config": {
            "image_caption_provider_id": None,  # 非字符串 → 回退 ""
            "image_caption_prompt": 123,  # 非字符串 → 回退 ""
            "image_caption_concurrency": "3",  # LLMService 内 int 转换
            "image_caption_enabled": True,
        },
    }
    plugin = let_me_check(SimpleNamespace(), config)
    assert plugin.max_messages == 200
    assert plugin.parse_nested_forward is True
    assert plugin.max_nested_depth == 0
    assert plugin.model_config["image_caption_provider_id"] == ""
    assert plugin.model_config["image_caption_prompt"] == ""
    assert plugin._llm._caption_concurrency == 3


def test_extreme_config_values_clamped(make_event):
    """超大/非数字配置裁剪到合理范围。"""
    plugin = let_me_check(
        SimpleNamespace(),
        {
            "max_messages": 99999,
            "max_nested_depth": "abc",
            "model_config": {"image_caption_provider_id": ["bad"], "image_caption_prompt": None},
        },
    )
    assert plugin.max_messages == 2000  # 裁剪到上限
    assert plugin.max_nested_depth == 5  # 非数字 → 回退默认
    assert plugin.model_config["image_caption_provider_id"] == ""
    assert plugin.model_config["image_caption_prompt"] == ""


def test_injected_text_survives_core_save_filter(make_event):
    """P0-01 回归：模拟 AstrBot Core 保存历史时的 _no_save 过滤
    （dump_messages_with_checkpoints 丢弃 temp part），断言转发文本 part 存活、
    直通图片 part 被滤除——即转发内容随本轮历史持久化。"""
    plugin = make_plugin()

    async def scenario():
        umo = "aiocqhttp:GroupMessage:123456"
        fwd = make_event(
            segments=[Forward(id="fwd-1")], umo=umo, bot_payload=_fwd_payload("转发内容")
        )
        await plugin.on_message(fwd)
        trigger = make_event(segments=[Text(text="看看")], umo=umo)
        req = ProviderRequest()
        await plugin.on_llm_request(trigger, req)

        # 模拟 Core 保存过滤：丢弃 _no_save 的 part
        saved_parts = [
            p for p in req.extra_user_content_parts if not getattr(p, "_no_save", False)
        ]
        # 文字 part 必须存活（其 _no_save 为 False）
        assert len(saved_parts) == 1
        assert "<forwarded_message_context>" in saved_parts[0].text
        assert "转发内容" in saved_parts[0].text
        # 图片直通 part 仍为 temp（不写入历史）
        img_parts = [p for p in req.extra_user_content_parts if getattr(p, "_no_save", False)]
        assert all(getattr(p, "_no_save", False) for p in img_parts)

    asyncio.run(scenario())


def test_total_budget_truncates_by_chars(make_event):
    """总字符预算：超预算部分按入队顺序截断，文本末尾追加固定截断标记。"""
    plugin = make_plugin()

    async def scenario():
        umo = "aiocqhttp:GroupMessage:123456"
        for i in range(3):
            ev = make_event(
                segments=[Forward(id=f"fwd-{i}")],
                umo=umo,
                bot_payload=_fwd_payload(f"内容{i}" + "长" * 9000),
            )
            await plugin.on_message(ev)
        trigger = make_event(segments=[Text(text="总结")], umo=umo)
        req = ProviderRequest()
        await plugin.on_llm_request(trigger, req)

        text = req.extra_user_content_parts[0].text
        # 20000 字符预算：保留前 2 条（约 18000 字符），第 3 条截断
        assert "内容0" in text
        assert "内容1" in text
        assert "内容2" not in text
        assert "[内容已截断" in text
        assert umo not in plugin._pending

    asyncio.run(scenario())


def test_total_budget_truncates_by_images(make_event):
    """总图片预算：图片数超上限时按入队顺序截断，转述图片数受预算约束。"""
    plugin = make_plugin()
    # 补齐 _resolve_caption_provider 需要的 context 桩（无可用转述模型 → 占位符）
    plugin.context.get_provider_by_id = lambda pid: None
    plugin.context.get_config = lambda umo=None: {
        "provider_settings": {},
        "provider_ltm_settings": {},
    }

    def _img_payload(i):
        # 每条消息 2 张图片，共 12 条 = 24 张 > 20 张上限
        return {
            "messages": [
                {
                    "sender": {"nickname": "甲"},
                    "content": [
                        {"type": "image", "data": {"url": f"https://img.example.com/{i}-{j}.jpg"}}
                        for j in range(2)
                    ],
                }
                for i in range(12)
            ]
        }

    async def scenario():
        umo = "aiocqhttp:GroupMessage:123456"
        ev = make_event(segments=[Forward(data=_img_payload(0))], umo=umo)
        await plugin.on_message(ev)
        trigger = make_event(segments=[Text(text="总结")], umo=umo)
        req = ProviderRequest()
        await plugin.on_llm_request(trigger, req)

        text = req.extra_user_content_parts[0].text
        # 20 张图片预算：保留 10 条消息 × 2 张；无可用转述模型 → 占位符
        assert text.count("[图片: (图片)]") == 20
        assert "[内容已截断" in text
        assert umo not in plugin._pending

    asyncio.run(scenario())


def test_total_budget_under_limit_no_marker(make_event):
    """预算内：不追加截断标记，全部内容保留。"""
    plugin = make_plugin()

    async def scenario():
        umo = "aiocqhttp:GroupMessage:123456"
        for i in range(2):
            ev = make_event(
                segments=[Forward(id=f"fwd-{i}")],
                umo=umo,
                bot_payload=_fwd_payload(f"短内容{i}"),
            )
            await plugin.on_message(ev)
        trigger = make_event(segments=[Text(text="总结")], umo=umo)
        req = ProviderRequest()
        await plugin.on_llm_request(trigger, req)

        text = req.extra_user_content_parts[0].text
        assert "短内容0" in text
        assert "短内容1" in text
        assert "[内容已截断" not in text

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
