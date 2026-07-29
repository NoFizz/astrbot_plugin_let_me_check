"""
让我康康 - LLM 交互服务

负责：
- 图片转述（多模态模型调用，URL 去重，并发控制）
- 回复生成（使用 context.llm_generate 现代 API）
- Token 预算感知的 prompt 构建
"""

import asyncio

from astrbot.api import logger
from astrbot.core.provider.provider import Provider

from .models import ParsedMessage


class LLMService:
    """LLM 交互服务，封装所有模型调用逻辑"""

    def __init__(self, context, model_config: dict):
        self._context = context
        self._model_config = model_config
        self._caption_concurrency = max(
            1, int(model_config.get("image_caption_concurrency", 5))
        )

    # ─── 提供商获取 ─────────────────────────────────────────────

    def _get_chat_provider(self, umo: str) -> Provider | None:
        """获取对话模型提供商"""
        configured_id = self._model_config.get("provider_id", "").strip()
        if configured_id:
            provider = self._context.get_provider_by_id(configured_id)
            if provider and isinstance(provider, Provider):
                return provider
            if provider:
                logger.warning(
                    f"[SmartForward] 配置的提供商 '{configured_id}' 不是对话模型类型"
                )
        return self._context.get_using_provider(umo=umo)

    def _get_caption_provider(self, umo: str = "") -> Provider | None:
        """获取图片转述模型提供商。

        查找顺序：
        1. 插件配置的 image_caption_provider_id
        2. 全局配置 provider_settings.default_image_caption_provider_id
        3. 会话级配置 provider_ltm_settings.image_caption_provider_id
        4. 回退到当前会话的对话模型（多模态模型本身可处理图片）
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
            session_cfg = self._context.get_config(umo=umo) if umo else self._context.get_config()
        except Exception:
            session_cfg = self._context.get_config()
        caption_id = session_cfg.get("provider_settings", {}).get(
            "default_image_caption_provider_id", ""
        )
        if caption_id:
            provider = self._context.get_provider_by_id(caption_id)
            if provider and isinstance(provider, Provider):
                return provider

        # 3. 会话级配置（“群聊上下文感知 → 群聊图片转述模型”）
        ltm_cfg = session_cfg.get("provider_ltm_settings", {})
        session_caption_id = ltm_cfg.get("image_caption_provider_id", "")
        if session_caption_id:
            provider = self._context.get_provider_by_id(session_caption_id)
            if provider and isinstance(provider, Provider):
                return provider

        # 4. 回退到当前会话的对话模型（多模态模型本身可处理图片）
        if umo:
            try:
                provider = self._context.get_using_provider(umo=umo)
                if provider and isinstance(provider, Provider):
                    logger.info(
                        "[SmartForward] 未配置专用图片转述模型，回退使用当前对话模型"
                    )
                    return provider
            except Exception:
                pass

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

        provider = self._get_caption_provider(umo)
        if not provider:
            logger.info("[SmartForward] 未找到可用的图片转述模型，使用默认占位符")
            return ["(图片)"] * len(urls)

        # URL 去重：只转述唯一 URL
        unique_urls = list(dict.fromkeys(urls))  # 保序去重
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

    # ─── 回复生成 ─────────────────────────────────────────────

    async def generate_reply(self, prompt: str, umo: str) -> str | None:
        """调用 LLM 生成回复。

        优先使用 context.llm_generate()（尊重 persona/system_prompt），
        回退到 provider.text_chat()。
        """
        # 尝试使用现代 API
        try:
            provider_id = self._model_config.get("provider_id", "").strip()
            if not provider_id:
                provider_id = await self._context.get_current_chat_provider_id(umo=umo)

            if provider_id and hasattr(self._context, "llm_generate"):
                resp = await asyncio.wait_for(
                    self._context.llm_generate(
                        chat_provider_id=provider_id, prompt=prompt
                    ),
                    timeout=60,
                )
                if resp and resp.completion_text:
                    return resp.completion_text
        except Exception as e:
            logger.warning(
                f"[SmartForward] llm_generate 调用失败，回退到 text_chat: {e}"
            )

        # 回退路径
        provider = self._get_chat_provider(umo)
        if not provider:
            logger.error("[SmartForward] 未找到可用的LLM提供商")
            return None

        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt), timeout=60
            )
            if response and response.completion_text:
                return response.completion_text
        except Exception as e:
            logger.error(f"[SmartForward] LLM 调用失败: {type(e).__name__}: {e}")

        return None

    # ─── Prompt 构建 ─────────────────────────────────────────────

    def build_prompt(
        self,
        messages: list[ParsedMessage],
        image_descriptions: list[str],
        sender_name: str,
        is_group: bool,
        group_id: str = "",
    ) -> str:
        """构建 LLM 提示词（内容已由 max_messages 限制）"""
        chat_type = "群聊" if is_group else "私聊"
        group_info = f"，来自群组 {group_id}" if is_group else ""

        chat_lines = self._build_chat_lines(messages, image_descriptions)
        chat_content = "\n".join(chat_lines)

        prompt = (
            f"你正在帮用户分析一条{chat_type}中的合并转发消息。"
            f"这条消息由 {sender_name} 转发{group_info}。"
            f"转发消息中包含以下聊天记录：\n\n"
            f"{chat_content}\n\n"
            f"请根据以上转发的聊天记录内容，给出自然、口语化的分析或总结。"
            f"像朋友之间聊天一样随意，不要加'根据聊天记录'之类的前缀。"
        )
        return prompt

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
