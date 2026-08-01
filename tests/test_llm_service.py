"""Tests for llm_service.py: caption provider resolution, image captioning,
chat line building and reply generation."""

import asyncio
from types import SimpleNamespace

from astrbot.api import logger

from astrbot_plugin_let_me_check.llm_service import LLMService
from astrbot_plugin_let_me_check.models import ParsedMessage

from astrbot.core.provider.provider import Provider


class FakeProvider(Provider):
    def __init__(self, response_text="描述"):
        super().__init__()
        self.calls = []
        self.response_text = response_text
        self.pid = ""
        self.model_name = ""
        self.provider_config = {"id": ""}

    async def text_chat(self, prompt="", image_urls=None):
        self.calls.append({"prompt": prompt, "image_urls": image_urls or []})
        return SimpleNamespace(completion_text=self.response_text)


def make_context(provider=None, ltm_provider=None, provider_ltm_settings=None):
    def get_provider_by_id(pid):
        for p in (provider, ltm_provider):
            if p and pid == p.pid:
                return p
        return None

    return SimpleNamespace(
        get_provider_by_id=get_provider_by_id,
        get_using_provider=lambda umo=None: provider,
        get_config=lambda umo=None: {
            "provider_settings": {},
            "provider_ltm_settings": provider_ltm_settings or {},
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


def test_supports_image_input_semantics():
    """_supports_image_input：modalities 含 image 或未配置 → True；否则 False。"""
    svc = LLMService(SimpleNamespace(), {})

    class _NoModalities:
        provider_config = {"id": "p1"}  # 未配置 → 默认支持所有模态

    assert svc._supports_image_input(_NoModalities()) is True

    class _WithImage:
        provider_config = {"id": "p2", "modalities": ["text", "image"]}

    assert svc._supports_image_input(_WithImage()) is True

    class _TextOnly:
        provider_config = {"id": "p3", "modalities": ["text", "tool_use"]}

    assert svc._supports_image_input(_TextOnly()) is False


def test_caption_provider_skips_text_only_fallback_model():
    """回退的对话模型不支持图片输入（modalities 无 image）→ 跳过，不盲目调用。"""
    provider = FakeProvider()
    provider.provider_config = {"id": "chat-1", "modalities": ["text"]}
    context = make_context(provider)
    svc = LLMService(context, {})
    assert svc._get_caption_provider("umo-x") is None
    warnings = [msg for lvl, msg in logger.logs if lvl == "warning"]
    assert any("不支持图片输入" in msg for msg in warnings)


def test_caption_provider_prefers_ltm_model_over_errors():
    """对话模型不支持图片输入时，回退链第 4 级使用'群聊图片转述模型'（ltm 配置）。"""
    chat = FakeProvider()
    chat.provider_config = {"id": "chat-1", "modalities": ["text"]}  # 不支持图片
    ltm = FakeProvider()
    ltm.pid = "ltm-cap"
    ltm.provider_config = {"id": "ltm-cap", "modalities": ["text", "image"]}
    context = make_context(
        provider=chat,
        ltm_provider=ltm,
        provider_ltm_settings={"image_caption_provider_id": "ltm-cap"},
    )
    svc = LLMService(context, {})
    assert svc._get_caption_provider("umo-x") is ltm


def test_caption_provider_errors_when_all_levels_unavailable():
    """回退链全部不可用（含 ltm 未配置）→ 返回 None 且日志出现 error。"""
    chat = FakeProvider()
    chat.provider_config = {"id": "chat-1", "modalities": ["text"]}  # 不支持图片
    context = make_context(provider=chat)
    svc = LLMService(context, {})
    assert svc._get_caption_provider("umo-x") is None
    errors = [msg for lvl, msg in logger.logs if lvl == "error"]
    assert any("未配置" in msg and "群聊图片转述模型" in msg for msg in errors)


def test_resolve_caption_provider_reports_level():
    """_resolve_caption_provider 返回 (provider, level)：1=插件 2=全局 3=对话模型 4=ltm 0=无。"""
    # level 1: 插件配置命中
    plug = FakeProvider()
    plug.pid = "plug-cap"
    plug.provider_config = {"id": "plug-cap", "modalities": ["image"]}
    ctx1 = make_context(provider=plug)
    svc1 = LLMService(ctx1, {"image_caption_provider_id": "plug-cap"})
    p1, l1 = svc1._resolve_caption_provider("umo-x")
    assert p1 is plug and l1 == 1

    # level 3: 当前对话模型（支持图片输入）
    chat = FakeProvider()
    chat.provider_config = {"id": "chat-1", "modalities": ["image"]}
    ctx3 = make_context(provider=chat)
    svc3 = LLMService(ctx3, {})
    p3, l3 = svc3._resolve_caption_provider("umo-x")
    assert p3 is chat and l3 == 3

    # level 0: 全部不可用（对话模型不支持图片、无 ltm）
    chat0 = FakeProvider()
    chat0.provider_config = {"id": "chat-0", "modalities": ["text"]}
    ctx0 = make_context(provider=chat0)
    svc0 = LLMService(ctx0, {})
    p0, l0 = svc0._resolve_caption_provider("umo-x")
    assert p0 is None and l0 == 0


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


def test_provider_label_formats_identifiers():
    """_provider_label 输出完整 id（提供商/型号）；meta() 优先；无属性回退类名。"""
    from astrbot_plugin_let_me_check.llm_service import _provider_label

    class _MetaProvider:
        def meta(self):
            return SimpleNamespace(id="p1", model="m1")

    assert _provider_label(_MetaProvider()) == "p1"

    class _CfgProvider:
        provider_config = {"id": "openai-cfg"}
        model_name = "gpt-4o"

    assert _provider_label(_CfgProvider()) == "openai-cfg"

    class _Bare:
        pass

    assert _provider_label(_Bare()) == "_Bare"


def test_describe_images_logs_caption_provider_model():
    """转述图片时日志记录正在使用的图片转述模型（完整 id）。"""
    provider = FakeProvider()
    provider.pid = "cap-1"
    provider.model_name = "test-caption-model"
    provider.provider_config = {"id": "cap-1"}
    context = make_context(provider)
    svc = LLMService(
        context,
        {"image_caption_provider_id": "cap-1", "image_caption_concurrency": 2},
    )
    asyncio.run(svc.describe_images(["https://a/1.jpg"], "描述吧", umo="u"))
    infos = [msg for lvl, msg in logger.logs if lvl == "info"]
    assert any(
        "图片转述模型" in msg and "cap-1" in msg
        for msg in infos
    )
