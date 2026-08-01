"""
让我康康 - LLM 交互服务

负责：
- 图片转述（多模态模型调用，URL 去重，并发控制）
- Token 预算感知的 prompt 构建
"""

import asyncio

from astrbot.api import logger
from astrbot.core.provider.provider import Provider

from .models import ParsedMessage


def _provider_label(provider) -> str:
    """返回 provider 的可读标识（提供商 id / 模型名），用于日志。

    防御式获取：真实 Provider 具备 meta()（ProviderMeta: id/model/type），
    未知形状对象依次回退 provider_config.id、model_name，最终用类名兜底。
    """
    try:
        meta = provider.meta()
        return f"{meta.id} / {meta.model}"
    except Exception:
        pass
    cfg = getattr(provider, "provider_config", None)
    pid = cfg.get("id", "") if isinstance(cfg, dict) else ""
    model = getattr(provider, "model_name", "") or ""
    if pid and model:
        return f"{pid} / {model}"
    return pid or model or type(provider).__name__


class LLMService:
    """LLM 交互服务，封装所有模型调用逻辑"""

    def __init__(self, context, model_config: dict):
        self._context = context
        self._model_config = model_config
        raw_concurrency = model_config.get("image_caption_concurrency", 5)
        try:
            caption_concurrency = int(raw_concurrency)
        except (TypeError, ValueError):
            logger.warning(
                f"[SmartForward] image_caption_concurrency 配置值 {raw_concurrency!r} "
                f"无效，已回退到默认值 5"
            )
            caption_concurrency = 5
        self._caption_concurrency = max(1, caption_concurrency)
        self._image_caption_enabled = bool(
            model_config.get("image_caption_enabled", True)
        )

    # ─── 提供商获取 ─────────────────────────────────────────────

    def _supports_image_input(self, provider) -> bool:
        """判断 provider 是否支持图片输入。

        依据 provider_config["modalities"]（WebUI"模型能力"配置）：
        - 未配置（None/空）→ 默认支持所有模态（与 AstrBot core 语义一致）
        - 配置了 → 仅当包含 "image" 时支持
        """
        modalities = getattr(provider, "provider_config", {}).get("modalities")
        if not modalities:
            return True
        return "image" in modalities

    def _get_caption_provider(self, umo: str = "") -> Provider | None:
        """获取图片转述模型提供商。

        查找顺序：
        1. 插件配置的 image_caption_provider_id
        2. 全局配置 provider_settings.default_image_caption_provider_id
        3. 当前会话的对话模型（仅当支持图片输入，modalities 含 image 或未配置）
        4. 会话级配置 provider_ltm_settings.image_caption_provider_id（最终兜底）
           若仍未配置，记录 error 日志并返回 None
        """
        # 1. 插件自身配置
        configured_id = self._model_config.get("image_caption_provider_id", "").strip()
        if configured_id:
            provider = self._context.get_provider_by_id(configured_id)
            if provider and isinstance(provider, Provider):
                return provider
            if provider:
                logger.warning(
                    f"[SmartForward] 配置的图片转述提供商 '{configured_id}' 不是对话模型类型"
                )

        # 2. 全局默认配置（“默认图片转述模型”）
        # 注意：必须传 umo 获取会话对应的配置文件，而非默认配置
        try:
            session_cfg = (
                self._context.get_config(umo=umo) if umo else self._context.get_config()
            )
        except Exception:
            session_cfg = self._context.get_config()
        caption_id = session_cfg.get("provider_settings", {}).get(
            "default_image_caption_provider_id", ""
        )
        if caption_id:
            provider = self._context.get_provider_by_id(caption_id)
            if provider and isinstance(provider, Provider):
                return provider

        # 3. 当前会话的对话模型（仅当支持图片输入）
        if umo:
            try:
                provider = self._context.get_using_provider(umo=umo)
                if provider and isinstance(provider, Provider):
                    if self._supports_image_input(provider):
                        logger.info(
                            "[SmartForward] 未配置专用图片转述模型，当前对话模型支持图片输入，直接复用于图片转述"
                        )
                        return provider
                    logger.warning(
                        f"[SmartForward] 当前对话模型 {_provider_label(provider)} 不支持图片输入，"
                        "继续查找下一级"
                    )
            except Exception as e:
                logger.warning(
                    f"[SmartForward] 获取当前对话模型失败: {type(e).__name__}: {e}"
                )

        # 4. 会话级配置（“群聊上下文感知 → 群聊图片转述模型”）——最终兜底
        ltm_cfg = session_cfg.get("provider_ltm_settings", {})
        session_caption_id = ltm_cfg.get("image_caption_provider_id", "")
        if session_caption_id:
            provider = self._context.get_provider_by_id(session_caption_id)
            if provider and isinstance(provider, Provider):
                return provider

        logger.error(
            "[SmartForward] 未找到可用的图片转述模型：插件配置、默认图片转述模型、"
            "当前对话模型（不支持图片输入）均不可用，且未配置群聊图片转述模型"
        )
        return None

    # ─── 图片转述 ─────────────────────────────────────────────

    async def describe_images(
        self, urls: list[str], prompt: str, umo: str = ""
    ) -> list[str]:
        """使用多模态模型描述图片内容。

        改进：
        - URL 去重：相同 URL 只调用一次模型
        - 并发数可配置
        - 结果按原始 URL 顺序映射
        """
        if not urls:
            return []

        if not self._image_caption_enabled:
            logger.info("[SmartForward] 图片转述已关闭，使用占位符")
            return ["(图片)"] * len(urls)

        provider = self._get_caption_provider(umo)
        if not provider:
            logger.info("[SmartForward] 未找到可用的图片转述模型，使用默认占位符")
            return ["(图片)"] * len(urls)

        # URL 去重：只转述唯一 URL
        unique_urls = list(dict.fromkeys(urls))  # 保序去重
        logger.info(
            f"[SmartForward] 正在使用图片转述模型解析 {len(unique_urls)} 张图片 | "
            f"提供商: {_provider_label(provider)}"
        )
        sem = asyncio.Semaphore(self._caption_concurrency)
        caption_map: dict[str, str] = {}

        async def _caption_one(url: str) -> None:
            async with sem:
                try:
                    response = await provider.text_chat(prompt=prompt, image_urls=[url])
                    if response and response.completion_text:
                        caption_map[url] = response.completion_text.strip()
                    else:
                        caption_map[url] = "(图片)"
                except Exception as e:
                    logger.warning(
                        f"[SmartForward] 图片描述失败: {type(e).__name__}: {e}"
                    )
                    caption_map[url] = "(图片)"

        await asyncio.gather(*[_caption_one(url) for url in unique_urls])

        # 按原始顺序映射结果
        return [caption_map.get(url, "(图片)") for url in urls]

    def build_user_message_text(
        self,
        messages: list[ParsedMessage],
        image_descriptions: list[str],
        sender_name: str,
        is_group: bool,
    ) -> str:
        """构建写入会话历史的完整用户消息文本"""
        chat_type = "群聊" if is_group else "私聊"
        chat_lines = self._build_chat_lines(messages, image_descriptions)
        chat_content = "\n".join(chat_lines)
        return f"[转发消息] {sender_name} 转发了一条{chat_type}消息，内容如下：\n{chat_content}"

    # ─── 内部辅助 ─────────────────────────────────────────────

    def _build_chat_lines(
        self, messages: list[ParsedMessage], image_descriptions: list[str]
    ) -> list[str]:
        """构建聊天行列表，将图片占位符替换为描述"""
        lines = []
        desc_idx = 0
        for msg in messages:
            line_content = msg.content
            if msg.image_count > 0:
                for _ in range(msg.image_count):
                    if desc_idx < len(image_descriptions):
                        line_content = line_content.replace(
                            "[图片]", f"[图片: {image_descriptions[desc_idx]}]", 1
                        )
                        desc_idx += 1
            if msg.has_video:
                line_content += " [视频]"
            lines.append(f"{msg.sender}: {line_content}")
        return lines
