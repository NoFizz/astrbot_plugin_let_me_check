"""Tests for llm_service.py: caption provider resolution, image captioning,
chat line building and reply generation."""

import asyncio
from types import SimpleNamespace

from astrbot_plugin_let_me_check.llm_service import LLMService
from astrbot_plugin_let_me_check.models import ParsedMessage

from astrbot.core.provider.provider import Provider


class FakeProvider(Provider):
    def __init__(self, response_text="描述"):
        super().__init__()
        self.calls = []
        self.response_text = response_text

    async def text_chat(self, prompt="", image_urls=None):
        self.calls.append({"prompt": prompt, "image_urls": image_urls or []})
        return SimpleNamespace(completion_text=self.response_text)


def make_context(provider=None):
    return SimpleNamespace(
        get_provider_by_id=lambda pid: (
            provider if provider and pid == provider.pid else None
        ),
        get_using_provider=lambda umo=None: provider,
        get_config=lambda umo=None: {
            "provider_settings": {},
            "provider_ltm_settings": {},
        },
    )


def test_build_chat_lines_aligns_image_descriptions():
    svc = LLMService(SimpleNamespace(), {})
    msgs = [
        ParsedMessage(sender="A", content="hello [图片] world [图片]", image_count=2),
        ParsedMessage(sender="B", content="plain", image_count=0),
    ]
    lines = svc._build_chat_lines(msgs, ["图一", "图二"])
    assert lines[0] == "A: hello [图片: 图一] world [图片: 图二]"
    assert lines[1] == "B: plain"


def test_build_chat_lines_video_suffix():
    svc = LLMService(SimpleNamespace(), {})
    msgs = [ParsedMessage(sender="A", content="看", has_video=True)]
    lines = svc._build_chat_lines(msgs, [])
    assert lines[0] == "A: 看 [视频]"


def test_describe_images_dedup_and_order():
    provider = FakeProvider()
    provider.pid = "cap-1"
    context = make_context(provider)
    svc = LLMService(
        context,
        {"image_caption_provider_id": "cap-1", "image_caption_concurrency": 2},
    )
    result = asyncio.run(
        svc.describe_images(
            ["https://a/1.jpg", "https://a/2.jpg", "https://a/1.jpg"], "描述吧", umo="u"
        )
    )
    # URL 去重：相同 URL 只调用一次模型
    assert len(provider.calls) == 2
    assert [c["image_urls"][0] for c in provider.calls] == [
        "https://a/1.jpg",
        "https://a/2.jpg",
    ]
    # 结果按原始 URL 顺序映射（含重复）
    assert result == ["描述", "描述", "描述"]


def test_describe_images_no_provider_uses_placeholder():
    context = make_context(None)
    svc = LLMService(context, {})
    result = asyncio.run(svc.describe_images(["https://a/1.jpg"], "描述吧", umo="u"))
    assert result == ["(图片)"]


def test_caption_provider_falls_back_to_using_provider():
    provider = FakeProvider()
    context = make_context(provider)
    svc = LLMService(context, {})
    assert svc._get_caption_provider("umo-x") is provider


def test_invalid_caption_concurrency_falls_back_to_default():
    """非整数 image_caption_concurrency 配置回退到默认并发数 5。"""
    svc_bad = LLMService(SimpleNamespace(), {"image_caption_concurrency": "abc"})
    assert svc_bad._caption_concurrency == 5
    svc_none = LLMService(SimpleNamespace(), {"image_caption_concurrency": None})
    assert svc_none._caption_concurrency == 5


def test_describe_images_disabled_returns_placeholder_without_provider_call():
    """image_caption_enabled=False 时：返回占位符，且不调用任何模型。"""
    provider = FakeProvider()
    provider.pid = "cap-1"
    context = make_context(provider)
    svc = LLMService(
        context,
        {
            "image_caption_provider_id": "cap-1",
            "image_caption_concurrency": 2,
            "image_caption_enabled": False,
        },
    )
    result = asyncio.run(
        svc.describe_images(["https://a/1.jpg", "https://a/2.jpg"], "描述吧", umo="u")
    )
    assert result == ["(图片)", "(图片)"]
    assert provider.calls == []  # 未调用任何模型
