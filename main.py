"""
让我康康 v2.0.1 - AstrBot 合并转发消息智能分析插件

功能：
- 自动解析QQ合并转发消息内容（支持嵌套转发）
- 群聊/私聊独立开关与白名单控制
- 结合当前会话上下文，调用LLM生成自然回复
- 支持图片内容转述（需配置多模态模型）
- 回复模式可选：注入主管道 / 主动回复

作者: NoFizz
版本: 2.0.1
许可证: AGPL-3.0
"""

import asyncio
import hashlib
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

from .history import HistoryManager
from .llm_service import LLMService
from .models import ProcessingResult
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


@register(
    "let_me_check",
    "NoFizz",
    "智能分析QQ合并转发消息，支持群聊/私聊独立开关与白名单，结合会话上下文自动回复",
    "2.0.1",
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

        # 管道协调状态
        self._processing_events: dict[str, asyncio.Event] = {}
        self._forward_results: dict[
            str, tuple[str, float]
        ] = {}  # umo -> (result, timestamp)
        self._result_ttl = 120  # 结果保留秒数

        # per-UMO 并发保护
        self._umo_locks: dict[str, asyncio.Lock] = {}

        # 后台任务引用
        self._background_tasks: set = set()

    def _load_config(self):
        """加载配置项"""
        self.group_chat = self.config.get("group_chat", {})
        self.private_chat = self.config.get("private_chat", {})
        self.max_messages = self.config.get("max_messages", 200)
        self.parse_nested_forward = self.config.get("parse_nested_forward", True)
        self.max_nested_depth = self.config.get("max_nested_depth", 3)
        self.model_config = self.config.get("model_config", {})
        self.reply_mode = self.model_config.get("reply_mode", "inject_only")

    # ─── 事件处理 ─────────────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """主管道 LLM 请求钩子：若插件正在处理转发，等待完成后注入内容"""
        if self.reply_mode == "proactive_only":
            return

        umo = event.unified_msg_origin
        proc_event = self._processing_events.get(umo)
        if not proc_event:
            return

        logger.info("[SmartForward] 主管道等待转发处理完成...")
        try:
            await asyncio.wait_for(proc_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("[SmartForward] 等待转发处理超时，主管道继续执行")

        # 注入转发内容（mark_as_temp 防止主管道二次持久化，
        # 转发内容已由插件通过 write_forward_pair 写入会话历史）
        result_entry = self._forward_results.pop(umo, None)
        if result_entry:
            result_text, _ = result_entry
            from astrbot.core.agent.message import TextPart

            req.extra_user_content_parts.append(
                TextPart(
                    text=f"<forwarded_message_context>\n{result_text}\n</forwarded_message_context>"
                ).mark_as_temp()
            )
            logger.info("[SmartForward] 已将转发内容注入主管道请求")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息处理器 - 检测合并转发消息"""
        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            return

        umo = event.unified_msg_origin
        is_group = bool(event.get_group_id())

        enabled = self._is_enabled_for_chat(umo, is_group)
        if not enabled:
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

        # per-UMO 并发保护：同一会话同时只处理一条转发
        # 先检查已注册的未完成 Event（同步段，无调度窗口竞态）：
        # 若仅依赖 lock.locked()，相邻两条消息可能因 create_task 调度延迟
        # 而双双通过检查，导致 _processing_events[umo] 被覆盖、前一条结果丢失。
        existing = self._processing_events.get(umo)
        if existing is not None and not existing.is_set():
            logger.info("[SmartForward] 该会话正在处理另一条转发，跳过")
            return
        lock = self._umo_locks.setdefault(umo, asyncio.Lock())
        if lock.locked():
            logger.info("[SmartForward] 该会话正在处理另一条转发，跳过")
            return

        # 注册 Event（在 create_task 之前，确保主管道钩子能立即看到）
        proc_event = asyncio.Event()
        self._processing_events[umo] = proc_event

        task = asyncio.create_task(
            self._process_forward(event, detect_result, umo, is_group, proc_event, lock)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ─── 核心处理流程 ─────────────────────────────────────────────

    async def _process_forward(
        self,
        event: AstrMessageEvent,
        detect_result,
        umo: str,
        is_group: bool,
        proc_event: asyncio.Event,
        lock: asyncio.Lock,
    ):
        """处理合并转发消息的完整流程"""
        async with lock:
            try:
                # 1. 提取消息
                logger.info("[SmartForward] 开始解析转发内容...")
                result: ProcessingResult = await asyncio.wait_for(
                    extract_messages(
                        event,
                        detect_result,
                        max_messages=self.max_messages,
                        parse_nested=self.parse_nested_forward,
                        max_depth=self.max_nested_depth,
                    ),
                    timeout=30,
                )

                if not result.messages:
                    logger.info("[SmartForward] 转发内容为空，跳过")
                    return

                logger.info(
                    f"[SmartForward] 解析完成: {len(result.messages)} 条消息, {len(result.image_urls)} 张图片"
                )

                # 2. 图片转述（URL 去重在 LLMService 内部处理）
                image_descriptions = []
                if result.image_urls:
                    caption_prompt = (
                        self.model_config.get("image_caption_prompt", "").strip()
                        or "请用中文简短描述这张图片的内容。"
                    )
                    logger.info(
                        f"[SmartForward] 开始图片转述 ({len(result.image_urls)} 张)..."
                    )
                    image_descriptions = await asyncio.wait_for(
                        self._llm.describe_images(
                            result.image_urls, caption_prompt, umo=umo
                        ),
                        timeout=120,
                    )
                    logger.info("[SmartForward] 图片转述完成")

                # 3. 构建完整文本
                sender_name = event.get_sender_name()
                user_message_text = self._llm.build_user_message_text(
                    result.messages, image_descriptions, sender_name, is_group
                )

                # 4. 存储结果并通知主管道
                if self._processing_events.get(umo) is proc_event:
                    self._forward_results[umo] = (user_message_text, time.time())
                proc_event.set()
                logger.info("[SmartForward] 转发处理完成，已通知主管道")

                # 5. 根据 reply_mode 决定是否主动回复
                reply_text = ""
                if self.reply_mode == "proactive_only":
                    group_id = event.get_group_id() or ""
                    prompt = self._llm.build_prompt(
                        result.messages,
                        image_descriptions,
                        sender_name,
                        is_group,
                        group_id=group_id,
                    )
                    logger.info(
                        f"[SmartForward] 调用LLM分析 (prompt长度={len(prompt)})..."
                    )
                    reply_text = await self._llm.generate_reply(prompt, umo) or ""

                    if reply_text:
                        msg = MessageChain().message(reply_text)
                        await self.context.send_message(umo, msg)
                        logger.info(f"[SmartForward] 回复成功: {reply_text[:50]}...")
                    else:
                        logger.warning("[SmartForward] LLM返回无效响应，跳过回复")

                # 6. 一次性写入会话历史（原子操作）
                await self._history.write_forward_pair(
                    umo, user_message_text, reply_text
                )

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.error("[SmartForward] 处理超时（解析/转述/LLM某一环节超时）")
                self._remove_dedup(detect_result.forward_id, umo)
            except Exception as e:
                logger.error(
                    f"[SmartForward] 处理转发消息失败: {type(e).__name__}: {e}"
                )
                self._remove_dedup(detect_result.forward_id, umo)
            finally:
                if not proc_event.is_set():
                    proc_event.set()
                if self._processing_events.get(umo) is proc_event:
                    self._processing_events.pop(umo, None)
                self._cleanup_expired_results()

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
        """处理失败时移除去重标记，允许重试"""
        content = f"{forward_id}:{umo}"
        msg_hash = hashlib.md5(content.encode()).hexdigest()
        self._dedup_cache.pop(msg_hash, None)

    def _cleanup_expired_results(self):
        """清理过期的转发结果（替代 call_later）"""
        now = time.time()
        expired = [
            k
            for k, (_, ts) in self._forward_results.items()
            if now - ts > self._result_ttl
        ]
        for k in expired:
            self._forward_results.pop(k, None)

    # ─── 生命周期 ─────────────────────────────────────────────

    async def terminate(self):
        """插件终止时资源释放"""
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        for evt in self._processing_events.values():
            evt.set()
        self._processing_events.clear()
        self._forward_results.clear()
        self._umo_locks.clear()
        logger.info("[SmartForward] 插件已停止")
