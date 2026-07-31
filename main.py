"""
让我康康 v2.1.0 - AstrBot 合并转发消息智能分析插件

功能：
- 按需解析：收到合并转发消息仅暂存（零 Token 消耗），LLM 触发时统一解析注入
- 纯转发消息（私聊）不触发 LLM，等待用户后续消息一并解析
- 支持嵌套转发解析与图片内容转述（可开关）
- 群聊/私聊独立开关与白名单控制

作者: NoFizz
版本: 2.1.0
许可证: AGPL-3.0
"""

import asyncio
import hashlib
import time

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

from .history import HistoryManager
from .llm_service import LLMService
from .models import ParsedMessage, ProcessingResult
from .parser import detect_forward, extract_messages

# 平台适配检查
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )

    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False
    AiocqhttpMessageEvent = None

# 暂存队列常量（不进配置 schema）
_PENDING_MAX = 10  # 每会话暂存上限
_PENDING_TTL = 86400  # 暂存有效期 24h（秒）
_EXTRACT_TIMEOUT = 30  # 单条转发解析超时
_CAPTION_TIMEOUT = 120  # 图片转述超时


@register(
    "let_me_check",
    "NoFizz",
    "智能分析QQ合并转发消息，支持群聊/私聊独立开关与白名单，按需解析注入会话上下文",
    "2.1.0",
    "https://github.com/NoFizz/astrbot_plugin_let_me_check",
)
class SmartForward(Star):
    """智能合并转发消息分析插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._load_config()

        # 服务模块
        self._llm = LLMService(context, self.model_config)
        self._history = HistoryManager(context)

        # 去重缓存
        self._dedup_cache: dict[str, float] = {}
        self._dedup_ttl = 3600
        self._dedup_max_size = 1024

        # 按需解析暂存队列: umo -> [(event, detect_result, timestamp)]
        self._pending: dict[str, list] = {}

    def _load_config(self):
        """加载配置项"""
        self.group_chat = self.config.get("group_chat", {})
        self.private_chat = self.config.get("private_chat", {})
        self.max_messages = self.config.get("max_messages", 200)
        self.parse_nested_forward = self.config.get("parse_nested_forward", True)
        self.max_nested_depth = self.config.get("max_nested_depth", 3)
        self.model_config = self.config.get("model_config", {})

    # ─── 事件处理 ─────────────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """主管道 LLM 请求钩子：按需解析暂存的转发消息。

        纯转发消息（消息链中仅含 Forward/Nodes）调用 event.stop_event()
        吞掉本次 LLM 调用——该转发已在 on_message 中入队，等待后续消息触发。
        含实质内容的消息（文本/图片/At 等）取出该会话全部暂存，
        逐条解析后合并注入请求，并将合并文本写入会话历史。
        """
        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            return

        umo = event.unified_msg_origin

        # 纯转发：中止本次 LLM 调用，转发已由 on_message 入队
        if self._is_pure_forward(event):
            event.stop_event()
            logger.info(
                "[SmartForward] 纯转发消息，中止本次 LLM 调用（等待后续消息触发）"
            )
            return

        # 有实质内容 → 触发解析：取出全部暂存
        pending = self._drain_pending(umo)
        if not pending:
            return

        merged_text = await self._process_pending(umo, pending)
        if not merged_text:
            logger.error("[SmartForward] 全部暂存转发解析失败，跳过注入")
            return

        from astrbot.core.agent.message import TextPart

        # 注入（mark_as_temp 防止主管道二次持久化，
        # 转发内容已由插件通过 write_forward_pair 写入会话历史）
        req.extra_user_content_parts.append(
            TextPart(
                text=f"<forwarded_message_context>\n{merged_text}\n</forwarded_message_context>"
            ).mark_as_temp()
        )
        logger.info("[SmartForward] 已将暂存的转发内容注入主管道请求")
        await self._history.write_forward_pair(umo, merged_text, "")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息处理器 - 检测合并转发消息并暂存（不解析，零 Token 消耗）"""
        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            return

        umo = event.unified_msg_origin
        is_group = bool(event.get_group_id())

        if not self._is_enabled_for_chat(umo, is_group):
            return

        # 检测转发消息
        detect_result = detect_forward(event)
        if not detect_result:
            return

        logger.info(
            f"[SmartForward] 检测到转发消息 | id={detect_result.forward_id} | 来源={detect_result.source.value}"
        )

        # 去重检查
        if self._check_dedup(detect_result.forward_id, umo):
            logger.info(
                f"[SmartForward] 消息已处理过，跳过: {detect_result.forward_id[:20]}"
            )
            return

        # 仅暂存（不解析），等待 LLM 触发时统一解析
        self._enqueue_pending(umo, event, detect_result)
        logger.info("[SmartForward] 转发消息已暂存（按需解析，未消耗 Token）")

    # ─── 暂存队列 ─────────────────────────────────────────────

    def _enqueue_pending(self, umo: str, event, detect_result):
        """将转发暂存到会话队列，超限时淘汰最旧条目（FIFO）"""
        self._cleanup_expired_pending(umo)
        queue = self._pending.setdefault(umo, [])
        queue.append((event, detect_result, time.time()))
        if len(queue) > _PENDING_MAX:
            dropped = queue.pop(0)
            self._remove_dedup(dropped[1].forward_id, umo)
            logger.info(
                f"[SmartForward] 暂存队列超限，淘汰最旧条目: {dropped[1].forward_id[:20]}"
            )

    def _cleanup_expired_pending(self, umo: str):
        """惰性清理过期暂存条目（TTL 24h）"""
        queue = self._pending.get(umo)
        if not queue:
            return
        now = time.time()
        expired = [i for i, (_, _, ts) in enumerate(queue) if now - ts > _PENDING_TTL]
        for i in reversed(expired):
            self._remove_dedup(queue[i][1].forward_id, umo)
            queue.pop(i)
        if not queue:
            self._pending.pop(umo, None)

    def _drain_pending(self, umo: str) -> list:
        """取出并清空该会话的全部暂存条目"""
        self._cleanup_expired_pending(umo)
        return self._pending.pop(umo, [])

    def _is_pure_forward(self, event) -> bool:
        """判断当前消息是否为纯转发（消息链中仅含 Forward/Nodes 组件）。

        含任一非转发组件（text/image/at/reply 等）即视为含实质内容。
        """
        chain = getattr(event.message_obj, "message", None)
        if not chain:
            return False
        return all(isinstance(seg, (Comp.Forward, Comp.Nodes)) for seg in chain)

    # ─── 按需解析 ─────────────────────────────────────────────

    async def _process_pending(self, umo: str, pending: list) -> str | None:
        """逐条解析暂存转发并合并为注入文本。

        单条失败/为空：跳过该条并 _remove_dedup（允许重试），继续其余。
        全部失败：返回 None（纯转发场景已在 on_llm_request 顶部吞掉）。

        Args:
            umo: 统一消息来源标识
            pending: _drain_pending 返回的暂存条目列表

        Returns:
            合并后的用户消息文本，全部失败时返回 None
        """
        all_messages: list[ParsedMessage] = []
        all_image_urls: list[str] = []
        any_success = False

        for entry_event, detect_result, _ in pending:
            try:
                result: ProcessingResult = await asyncio.wait_for(
                    extract_messages(
                        entry_event,
                        detect_result,
                        max_messages=self.max_messages,
                        parse_nested=self.parse_nested_forward,
                        max_depth=self.max_nested_depth,
                    ),
                    timeout=_EXTRACT_TIMEOUT,
                )
            except Exception as e:
                logger.error(f"[SmartForward] 解析转发失败: {type(e).__name__}: {e}")
                self._remove_dedup(detect_result.forward_id, umo)
                continue
            if not result.messages:
                logger.info("[SmartForward] 转发内容为空，跳过")
                self._remove_dedup(detect_result.forward_id, umo)
                continue
            any_success = True
            all_messages.extend(result.messages)
            all_image_urls.extend(result.image_urls)

        if not any_success:
            return None

        # 图片转述（URL 去重在 LLMService 内部；开关关闭时返回占位符）
        image_descriptions: list[str] = []
        if all_image_urls:
            caption_prompt = (
                self.model_config.get("image_caption_prompt", "").strip()
                or "请用中文简短描述这张图片的内容。"
            )
            try:
                image_descriptions = await asyncio.wait_for(
                    self._llm.describe_images(all_image_urls, caption_prompt, umo=umo),
                    timeout=_CAPTION_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error("[SmartForward] 图片转述超时")
                image_descriptions = ["(图片)"] * len(all_image_urls)

        # 合并构建用户消息文本（多条转发合并为一段）
        sender_name = pending[0][0].get_sender_name()
        is_group = bool(pending[0][0].get_group_id())
        return self._llm.build_user_message_text(
            all_messages, image_descriptions, sender_name, is_group
        )

    # ─── 辅助方法 ─────────────────────────────────────────────

    def _is_enabled_for_chat(self, umo: str, is_group: bool) -> bool:
        """检查当前会话是否启用转发分析"""
        chat_config = self.group_chat if is_group else self.private_chat
        if not chat_config.get("enable", True):
            return False
        if not chat_config.get("whitelist_enable", False):
            return True
        whitelist = chat_config.get("whitelist", [])
        if not whitelist:
            return True
        return umo in whitelist

    def _check_dedup(self, forward_id: str, umo: str) -> bool:
        """检查消息是否已处理过"""
        content = f"{forward_id}:{umo}"
        msg_hash = hashlib.md5(content.encode()).hexdigest()
        now = time.time()

        if msg_hash in self._dedup_cache:
            if now < self._dedup_cache[msg_hash]:
                return True
            del self._dedup_cache[msg_hash]

        # 缓存超限时清理
        if len(self._dedup_cache) >= self._dedup_max_size:
            expired = [k for k, v in self._dedup_cache.items() if now >= v]
            for k in expired:
                del self._dedup_cache[k]
            if len(self._dedup_cache) >= self._dedup_max_size:
                oldest_key = min(self._dedup_cache, key=self._dedup_cache.get)
                del self._dedup_cache[oldest_key]

        self._dedup_cache[msg_hash] = now + self._dedup_ttl
        return False

    def _remove_dedup(self, forward_id: str, umo: str):
        """移除暂存标记（条目淘汰/解析失败时），允许重新暂存"""
        content = f"{forward_id}:{umo}"
        msg_hash = hashlib.md5(content.encode()).hexdigest()
        self._dedup_cache.pop(msg_hash, None)

    # ─── 生命周期 ─────────────────────────────────────────────

    async def terminate(self):
        """插件终止时资源释放"""
        self._pending.clear()
        logger.info("[SmartForward] 插件已停止")
