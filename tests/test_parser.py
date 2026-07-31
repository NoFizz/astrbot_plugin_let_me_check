"""Tests for parser.py: forward detection and message extraction."""

import asyncio
from types import SimpleNamespace

from astrbot.api.message_components import Forward, Nodes

from astrbot_plugin_let_me_check.models import ForwardDetectResult, ForwardSource
from astrbot_plugin_let_me_check.parser import (
    _extract_image_url,
    _nodes_to_raw_messages,
    detect_forward,
    extract_messages,
)


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
    payload = _forward_payload([{"sender": {"nickname": "A"}, "content": [_text_seg("hi")]}])
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
                "content": [_text_seg("你好"), _image_seg("https://img.example.com/1.jpg")],
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
    assert msg.has_image is True
    assert result.image_urls == ["https://img.example.com/1.jpg"]


def test_extract_messages_nested_forward(make_event):
    payload = _forward_payload(
        [
            {"sender": {"nickname": "B"}, "content": [_text_seg("外层")]},
            {"sender": {"nickname": "C"}, "content": [{"type": "forward", "data": {"id": "nested-1"}}]},
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
    assert _extract_image_url({"source_url": "https://a.com/y.jpg"}) == "https://a.com/y.jpg"
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
