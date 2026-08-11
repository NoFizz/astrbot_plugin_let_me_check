"""Tests for parser.py: forward detection and message extraction."""

import asyncio
from types import SimpleNamespace

import astrbot_plugin_let_me_check.parser as parser_mod
from astrbot_plugin_let_me_check.models import ForwardDetectResult, ForwardSource
from astrbot_plugin_let_me_check.parser import (
    _extract_image_url,
    _nodes_to_raw_messages,
    detect_forward,
    extract_messages,
)

from astrbot.api.message_components import Forward, Nodes


def _forward_payload(messages):
    return {"messages": messages}


def _text_seg(text):
    return {"type": "text", "data": {"text": text}}


def _image_seg(url):
    return {"type": "image", "data": {"url": url}}


# ── detect_forward ─────────────────────────────────────────────


def test_detect_forward_component_with_id(make_event):
    event = make_event(segments=[Forward(id="fwd-abc")])
    result = detect_forward(event)
    assert result is not None
    assert result.forward_id == "fwd-abc"
    assert result.source == ForwardSource.COMPONENT


def test_detect_forward_inline_payload_without_id(make_event):
    payload = _forward_payload(
        [{"sender": {"nickname": "A"}, "content": [_text_seg("hi")]}]
    )
    event = make_event(segments=[Forward(data=payload)])
    result = detect_forward(event)
    assert result is not None
    assert result.forward_id.startswith("inline_")
    assert result.forward_payload == payload
    assert result.source == ForwardSource.COMPONENT


def test_detect_forward_nodes(make_event):
    node = Nodes(nodes=[SimpleNamespace(name="A", uin="1", content=[])])
    event = make_event(segments=[node])
    result = detect_forward(event)
    assert result is not None
    assert result.source == ForwardSource.NODES
    assert result.forward_payload is not None


def test_detect_forward_no_forward_returns_none(make_event):
    event = make_event(segments=[])
    assert detect_forward(event) is None


def test_detect_forward_raw_message_fallback(make_event):
    raw = {"message": [{"type": "forward", "data": {"id": "raw-1"}}]}
    event = make_event(segments=[], raw_message=raw)
    result = detect_forward(event)
    assert result is not None
    assert result.forward_id == "raw-1"
    assert result.source == ForwardSource.RAW


# ── extract_messages ───────────────────────────────────────────


def test_extract_messages_text_and_image(make_event):
    payload = _forward_payload(
        [
            {
                "sender": {"nickname": "A"},
                "content": [
                    _text_seg("你好"),
                    _image_seg("https://img.example.com/1.jpg"),
                ],
            }
        ]
    )
    event = make_event(segments=[Forward(data=payload)])
    result = asyncio.run(extract_messages(event, detect_forward(event)))
    assert len(result.messages) == 1
    msg = result.messages[0]
    assert msg.sender == "A"
    assert msg.content == "你好[图片]"
    assert msg.image_count == 1
    assert msg.image_count == 1
    assert result.image_urls == ["https://img.example.com/1.jpg"]


def test_extract_messages_nested_forward(make_event):
    payload = _forward_payload(
        [
            {"sender": {"nickname": "B"}, "content": [_text_seg("外层")]},
            {
                "sender": {"nickname": "C"},
                "content": [{"type": "forward", "data": {"id": "nested-1"}}],
            },
        ]
    )
    nested_payload = {
        "data": {
            "messages": [{"sender": {"nickname": "D"}, "content": [_text_seg("内层")]}]
        }
    }
    event = make_event(
        segments=[Forward(data=payload)],
        bot_payload=nested_payload,
    )
    result = asyncio.run(extract_messages(event, detect_forward(event)))
    texts = [m.content for m in result.messages]
    assert "外层" in texts
    assert "内层" in texts
    # 嵌套内容插入在包含 forward 段的消息之前（外层在前）
    assert texts.index("外层") < texts.index("内层")


def test_extract_messages_max_messages_truncation(make_event):
    payload = _forward_payload(
        [
            {"sender": {"nickname": f"U{i}"}, "content": [_text_seg(f"m{i}")]}
            for i in range(5)
        ]
    )
    event = make_event(segments=[Forward(data=payload)])
    result = asyncio.run(extract_messages(event, detect_forward(event), max_messages=3))
    assert len(result.messages) == 3


def test_extract_messages_empty_forward(make_event):
    # 转发存在（有 id）但协议端返回空 payload → 空结果
    detect_result = ForwardDetectResult(
        forward_id="fwd-empty", forward_payload=None, source=ForwardSource.COMPONENT
    )
    event = make_event(segments=[Forward(id="fwd-empty")], bot_payload=None)
    result = asyncio.run(extract_messages(event, detect_result))
    assert result.messages == []
    assert result.image_urls == []


# ── helpers ────────────────────────────────────────────────────


def test_extract_image_url():
    assert _extract_image_url({"url": "https://a.com/x.jpg"}) == "https://a.com/x.jpg"
    assert (
        _extract_image_url({"source_url": "https://a.com/y.jpg"})
        == "https://a.com/y.jpg"
    )
    assert _extract_image_url({"file": "https://a.com/z.jpg"}) == "https://a.com/z.jpg"
    assert _extract_image_url({"file": "local/path.jpg"}) == ""
    assert _extract_image_url({"url": "ftp://a.com/z"}) == ""
    assert _extract_image_url({}) == ""


def test_nodes_to_raw_messages():
    nodes = [
        SimpleNamespace(
            name="A",
            uin="100",
            content=[
                SimpleNamespace(type="text", text="hi"),
                SimpleNamespace(type="Image", url="https://x/y.jpg", file="f.jpg"),
            ],
        )
    ]
    msgs = _nodes_to_raw_messages(nodes)
    assert len(msgs) == 1
    assert msgs[0]["sender"]["nickname"] == "A"
    assert msgs[0]["content"][0] == {"type": "text", "data": {"text": "hi"}}
    assert msgs[0]["content"][1]["type"] == "image"
    assert msgs[0]["content"][1]["data"]["url"] == "https://x/y.jpg"
    assert msgs[0]["content"][1]["data"]["file"] == "f.jpg"


def test_get_forward_msg_per_call_timeout(make_event):
    """单次 get_forward_msg 调用受 _GET_FORWARD_TIMEOUT 超时保护，超时返回空结果。"""

    class SlowBot:
        """call_action 耗时超过超时阈值的协议端 FakeBot。"""

        def __init__(self, forward_payload):
            self.api = self
            self.forward_payload = forward_payload

        async def call_action(self, action, **params):
            await asyncio.sleep(0.5)
            return self.forward_payload

    async def scenario():
        original = getattr(parser_mod, "_GET_FORWARD_TIMEOUT", None)
        parser_mod._GET_FORWARD_TIMEOUT = 0.05
        try:
            payload = _forward_payload(
                [{"sender": {"nickname": "A"}, "content": [_text_seg("hi")]}]
            )
            detect_result = ForwardDetectResult(
                forward_id="fwd-slow",
                forward_payload=None,
                source=ForwardSource.COMPONENT,
            )
            event = make_event(segments=[Forward(id="fwd-slow")])
            event.bot = SlowBot(payload)
            result = await extract_messages(event, detect_result)
            assert result.messages == []
        finally:
            if original is None:
                del parser_mod._GET_FORWARD_TIMEOUT
            else:
                parser_mod._GET_FORWARD_TIMEOUT = original

    asyncio.run(scenario())


def test_nested_fetch_concurrency_bounded(make_event):
    """嵌套转发并行获取受 _NESTED_FETCH_CONCURRENCY 上限约束，且内容保持完整。"""

    class CountingBot:
        """跟踪最大并发 call_action 调用数的协议端 FakeBot。"""

        def __init__(self):
            self.api = self
            self.active = 0
            self.max_active = 0
            self.total_calls = 0

        async def call_action(self, action, **params):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.total_calls += 1
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

    async def scenario():
        original = getattr(parser_mod, "_NESTED_FETCH_CONCURRENCY", None)
        parser_mod._NESTED_FETCH_CONCURRENCY = 4
        try:
            messages = [
                {
                    "sender": {"nickname": f"U{i}"},
                    "content": [{"type": "forward", "data": {"id": f"n{i}"}}],
                }
                for i in range(20)
            ]
            event = make_event(segments=[Forward(data=_forward_payload(messages))])
            event.bot = CountingBot()
            result = await extract_messages(event, detect_forward(event))
            assert event.bot.max_active <= 4
            texts = [m.content for m in result.messages]
            for i in range(20):
                assert f"nested-n{i}" in texts
        finally:
            if original is None:
                del parser_mod._NESTED_FETCH_CONCURRENCY
            else:
                parser_mod._NESTED_FETCH_CONCURRENCY = original

    asyncio.run(scenario())


def test_nested_forward_dedup_by_id(make_event):
    """同一嵌套 forward_id 被多处引用：只拉取一次（防环去重），内容不重复插入。"""

    class SameBot:
        """无论请求哪个 id 都返回同一转发内容的协议端 FakeBot。"""

        def __init__(self):
            self.api = self
            self.calls = []

        async def call_action(self, action, **params):
            nid = str(params.get("id", ""))
            self.calls.append(nid)
            return {
                "messages": [
                    {
                        "sender": {"nickname": f"S{nid}"},
                        "content": [_text_seg(f"same-{nid}")],
                    }
                ]
            }

    async def scenario():
        # 两条外层消息引用同一个嵌套转发 id "dup-1"
        payload = _forward_payload(
            [
                {"sender": {"nickname": "A"}, "content": [_text_seg("前")]},
                {"sender": {"nickname": "B"}, "content": [{"type": "forward", "data": {"id": "dup-1"}}]},
                {"sender": {"nickname": "C"}, "content": [{"type": "forward", "data": {"id": "dup-1"}}]},
            ]
        )
        bot = SameBot()
        event = make_event(segments=[Forward(data=payload)])
        event.bot = bot
        result = await extract_messages(event, detect_forward(event))
        # dup-1 只被拉取一次
        assert bot.calls.count("dup-1") == 1
        # 内容只插入一份（dedup 后保留首个实例）
        texts = [m.content for m in result.messages]
        assert texts.count("same-dup-1") == 1

    asyncio.run(scenario())


def test_nested_forward_self_reference_no_infinite_loop(make_event):
    """自引用嵌套转发（A 内含 A）：seen 去重保证不无限递归，正常返回。"""

    class SelfBot:
        """引用了自身 id 的协议端 FakeBot（返回的转发内容又包含自身）。"""

        def __init__(self):
            self.api = self
            self.calls = []

        async def call_action(self, action, **params):
            nid = str(params.get("id", ""))
            self.calls.append(nid)
            # 返回内容中又嵌套引用自身
            return {
                "messages": [
                    {
                        "sender": {"nickname": f"S{nid}"},
                        "content": [
                            _text_seg(f"level-{nid}"),
                            {"type": "forward", "data": {"id": nid}},
                        ],
                    }
                ]
            }

    async def scenario():
        payload = _forward_payload(
            [
                {"sender": {"nickname": "A"}, "content": [{"type": "forward", "data": {"id": "self-1"}}]},
            ]
        )
        bot = SelfBot()
        event = make_event(segments=[Forward(data=payload)])
        event.bot = bot
        result = await extract_messages(event, detect_forward(event))
        # self-1 只被拉取一次
        assert bot.calls.count("self-1") == 1
        assert len(result.messages) >= 1

    asyncio.run(scenario())


def test_total_forward_fetch_capped(make_event):
    """总 get_forward_msg 拉取次数受 _MAX_FORWARD_FETCH 上限约束，超限停止并 warning。"""

    class ManyBot:
        def __init__(self):
            self.api = self
            self.calls = 0

        async def call_action(self, action, **params):
            self.calls += 1
            nid = str(params.get("id", ""))
            return {
                "messages": [
                    {
                        "sender": {"nickname": f"N{nid}"},
                        "content": [_text_seg(f"nested-{nid}")],
                    }
                ]
            }

    async def scenario():
        original = getattr(parser_mod, "_MAX_FORWARD_FETCH", None)
        parser_mod._MAX_FORWARD_FETCH = 5  # 压到 5 便于测试
        try:
            # 10 个嵌套转发 → 若无闸门会拉 10 次
            messages = [
                {
                    "sender": {"nickname": f"U{i}"},
                    "content": [{"type": "forward", "data": {"id": f"n{i}"}}],
                }
                for i in range(10)
            ]
            bot = ManyBot()
            event = make_event(segments=[Forward(data=_forward_payload(messages))])
            event.bot = bot
            result = await extract_messages(event, detect_forward(event))
            assert bot.calls <= 5
        finally:
            if original is None:
                del parser_mod._MAX_FORWARD_FETCH
            else:
                parser_mod._MAX_FORWARD_FETCH = original

    asyncio.run(scenario())


def test_top_level_forward_self_reference_fetched_once(make_event):
    """远程顶层转发 A 的内容再次引用 A：顶层 ID 计入共享 _seen，
    只拉取一次，内容不重复插入。"""

    class SelfBot:
        def __init__(self):
            self.api = self
            self.calls = []

        async def call_action(self, action, **params):
            nid = str(params.get("id", ""))
            self.calls.append(nid)
            # 返回的顶层内容中又嵌套引用自身 id
            return {
                "messages": [
                    {
                        "sender": {"nickname": "S"},
                        "content": [
                            _text_seg("顶层内容"),
                            {"type": "forward", "data": {"id": nid}},
                        ],
                    }
                ]
            }

    async def scenario():
        bot = SelfBot()
        event = make_event(segments=[Forward(id="top-A")])
        event.bot = bot
        result = await extract_messages(event, detect_forward(event))
        # top-A 只被拉取一次（顶层占用 _seen 后，嵌套引用被去重）
        assert bot.calls.count("top-A") == 1
        texts = [m.content for m in result.messages]
        assert texts.count("顶层内容") == 1

    asyncio.run(scenario())


def test_default_nested_depth_is_five(make_event):
    """extract_messages 默认 max_depth 为 5（加深嵌套解析能力）。"""

    async def scenario():
        # 通过签名默认值验证（不实际构造 5 层转发）
        import inspect

        sig = inspect.signature(extract_messages)
        assert sig.parameters["max_depth"].default == 5

    asyncio.run(scenario())
