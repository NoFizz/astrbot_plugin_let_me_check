"""
QQ群转发解析 v1.0.0 - AstrBot 合并转发消息智能分析插件

功能：
- 自动解析QQ合并转发消息内容（支持嵌套转发）
- 群聊/私聊独立开关与白名单控制
- 结合当前会话上下文，调用LLM生成自然回复
- 支持图片内容转述（需配置多模态模型）
- 回复概率可配置（0.0-1.0）

作者: NoFizz
版本: 1.0.0
许可证: AGPL-3.0
"""

import json
import time
import random
import hashlib
import asyncio
from typing import List, Dict, Optional, Tuple, Any

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from astrbot.core.provider.provider import Provider
import astrbot.api.message_components as Comp

# 平台适配检查
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False
    AiocqhttpMessageEvent = None


@register(
    "smart_forward",
    "NoFizz",
    "智能分析QQ合并转发消息，支持群聊/私聊独立开关与白名单，结合会话上下文自动回复",
    "1.0.0",
    "https://github.com/NoFizz/astrbot_plugin_smart_forward"
)
class SmartForward(Star):
    """智能合并转发消息分析插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._load_config()
        self._dedup_cache: Dict[str, float] = {}
        self._dedup_ttl = 3600  # 1小时去重
        self._dedup_max_size = 1024  # 缓存最大条目数
        # 转发处理状态跟踪（用于让主管道等待插件完成）
        self._processing_events: Dict[str, asyncio.Event] = {}
        self._forward_results: Dict[str, str] = {}
        # 后台任务引用保持（防止 GC 回收）
        self._background_tasks: set = set()

    def _load_config(self):
        """加载配置项"""
        self.group_chat = self.config.get("group_chat", {})
        self.private_chat = self.config.get("private_chat", {})
        self.max_messages = self.config.get("max_messages", 200)
        self.parse_nested_forward = self.config.get("parse_nested_forward", True)
        self.max_nested_depth = self.config.get("max_nested_depth", 3)
        self.model_config = self.config.get("model_config", {})

    def _is_enabled_for_chat(self, umo: str, is_group: bool) -> Tuple[bool, float]:
        """检查当前会话是否启用转发分析，返回 (是否启用, 回复概率)"""
        chat_config = self.group_chat if is_group else self.private_chat

        if not chat_config.get("enable", True):
            return False, 0.0

        try:
            probability = max(0.0, min(1.0, float(chat_config.get("reply_probability", 1.0))))
        except (TypeError, ValueError):
            probability = 1.0

        if not chat_config.get("whitelist_enable", False):
            return True, probability

        whitelist = chat_config.get("whitelist", [])
        if not whitelist:
            return True, probability

        return umo in whitelist, probability

    def _check_dedup(self, forward_id: str, umo: str) -> bool:
        """检查消息是否已处理过"""
        content = f"{forward_id}:{umo}"
        msg_hash = hashlib.md5(content.encode()).hexdigest()
        now = time.time()

        if msg_hash in self._dedup_cache:
            if now < self._dedup_cache[msg_hash]:
                return True
            # 已过期，删除
            del self._dedup_cache[msg_hash]

        # 缓存超限时清理过期条目
        if len(self._dedup_cache) >= self._dedup_max_size:
            expired_keys = [k for k, v in self._dedup_cache.items() if now >= v]
            for k in expired_keys:
                del self._dedup_cache[k]
            # 若清理后仍超限，淘汰最旧的条目
            if len(self._dedup_cache) >= self._dedup_max_size:
                oldest_key = min(self._dedup_cache, key=self._dedup_cache.get)
                del self._dedup_cache[oldest_key]

        self._dedup_cache[msg_hash] = now + self._dedup_ttl
        return False

    def _get_provider(self, umo: str):
        """获取LLM提供商，优先使用配置的提供商，否则使用框架默认"""
        configured_id = self.model_config.get("provider_id", "").strip()
        if configured_id:
            provider = self.context.get_provider_by_id(configured_id)
            if provider and isinstance(provider, Provider):
                return provider
            if provider:
                logger.warning(f"[SmartForward] 配置的提供商 '{configured_id}' 不是对话模型类型")

        return self.context.get_using_provider(umo=umo)

    def _get_image_caption_provider(self):
        """获取图片转述模型提供商"""
        configured_id = self.model_config.get("image_caption_provider_id", "").strip()
        if configured_id:
            provider = self.context.get_provider_by_id(configured_id)
            if provider and isinstance(provider, Provider):
                return provider
            if provider:
                logger.warning(f"[SmartForward] 配置的图片转述提供商 '{configured_id}' 不是对话模型类型")

        astrbot_config = self.context.get_config()
        caption_id = astrbot_config.get("provider_settings", {}).get("default_image_caption_provider_id", "")
        if caption_id:
            provider = self.context.get_provider_by_id(caption_id)
            if provider and isinstance(provider, Provider):
                return provider

        return None

    async def _describe_images(self, image_urls: List[str]) -> List[str]:
        """使用图片转述模型描述图片内容（并行处理，带并发限制）"""
        provider = self._get_image_caption_provider()
        if not provider:
            logger.info("[SmartForward] 未找到可用的图片转述模型，使用默认占位符")
            return ["(图片)"] * len(image_urls)

        prompt = self.model_config.get("image_caption_prompt", "").strip() or "请用中文简短描述这张图片的内容。"
        sem = asyncio.Semaphore(5)  # 限制并发数，避免触发 Provider 速率限制

        async def _caption_one(url: str) -> str:
            async with sem:
                try:
                    response = await provider.text_chat(
                        prompt=prompt,
                        image_urls=[url],
                    )
                    if response and response.completion_text:
                        return response.completion_text.strip()
                    return "(图片)"
                except Exception as e:
                    logger.warning(f"[SmartForward] 图片描述失败: {type(e).__name__}: {e}")
                    return "(图片)"

        descriptions = await asyncio.gather(*[_caption_one(url) for url in image_urls])
        return list(descriptions)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """主管道 LLM 请求钩子：若插件正在处理转发，等待完成后再让主管道继续"""
        umo = event.unified_msg_origin
        proc_event = self._processing_events.get(umo)
        if not proc_event:
            return

        # 插件正在处理转发，等待完成（最多等60秒）
        # 注意：不检查 is_set()，因为 event 已 set 时 wait() 会立即返回
        logger.info("[SmartForward] 主管道等待转发处理完成...")
        try:
            await asyncio.wait_for(proc_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("[SmartForward] 等待转发处理超时，主管道继续执行")

        # 处理完成后，将完整转发内容注入到当前请求中
        result = self._forward_results.pop(umo, None)
        if result:
            from astrbot.core.agent.message import TextPart
            req.extra_user_content_parts.append(
                TextPart(text=f"<forwarded_message_context>\n{result}\n</forwarded_message_context>")
            )
            logger.info("[SmartForward] 已将转发内容注入主管道请求")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息处理器 - 检测合并转发消息"""
        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            return

        umo = event.unified_msg_origin
        is_group = bool(event.get_group_id())

        enabled, probability = self._is_enabled_for_chat(umo, is_group)
        if not enabled:
            return

        should_reply = probability > 0.0 and (probability >= 1.0 or random.random() <= probability)

        forward_id = None
        forward_payload = None

        try:
            # 检测消息链中的转发组件
            seg_types = [type(seg).__name__ for seg in event.message_obj.message]
            for seg in event.message_obj.message:
                # 检查 Forward 组件（标准路径）
                if isinstance(seg, Comp.Forward):
                    seg_data = getattr(seg, "data", {}) or {}
                    forward_id = (
                        getattr(seg, "id", None)
                        or getattr(seg, "resid", None)
                        or seg_data.get("id")
                        or seg_data.get("resid")
                        or seg_data.get("forward_id")
                    )
                    if isinstance(seg_data, dict) and isinstance(seg_data.get("messages"), list):
                        forward_payload = {"messages": seg_data.get("messages", [])}
                    if forward_id:
                        forward_id = str(forward_id)
                # 检查 Nodes 组件（部分协议端将转发解析为 Nodes）
                elif isinstance(seg, Comp.Nodes):
                    nodes = getattr(seg, "nodes", [])
                    if nodes:
                        forward_payload = {"messages": self._nodes_to_raw_messages(nodes)}

            # 如果标准组件未检测到，尝试从原始消息中查找 forward 段
            if not forward_id and not forward_payload:
                raw_msg = getattr(event.message_obj, "raw_message", None)
                if raw_msg:
                    raw_segments = (
                        raw_msg.get("message", [])
                        if isinstance(raw_msg, dict)
                        else getattr(raw_msg, "message", [])
                    )
                    if isinstance(raw_segments, list):
                        for rseg in raw_segments:
                            if not isinstance(rseg, dict):
                                continue
                            rtype = rseg.get("type", "")
                            if rtype in ("forward", "forward_msg", "nodes"):
                                rdata = rseg.get("data", {}) or {}
                                forward_id = (
                                    rdata.get("id")
                                    or rdata.get("resid")
                                    or rdata.get("forward_id")
                                )
                                if forward_id:
                                    forward_id = str(forward_id)
                                if isinstance(rdata.get("messages"), list):
                                    forward_payload = {"messages": rdata["messages"]}
                                logger.info(
                                    f"[SmartForward] 从原始消息检测到 forward 段 | type={rtype} | id={forward_id}"
                                )
                                break
        except Exception as e:
            logger.error(f"[SmartForward] 转发检测异常: {type(e).__name__}: {e}")
            return

        if not forward_id and not forward_payload:
            return

        logger.info(f"[SmartForward] 检测到转发消息 | id={forward_id} | 组件={seg_types}")

        if not forward_id and forward_payload:
            raw = json.dumps(forward_payload, ensure_ascii=False, sort_keys=True)
            forward_id = f"inline_{hashlib.md5(raw.encode()).hexdigest()[:16]}"

        if self._check_dedup(forward_id, umo):
            logger.info(f"[SmartForward] 消息已处理过，跳过: {forward_id[:20]}")
            return

        # 在 create_task 之前注册 Event，确保主管道钩子能立即看到
        proc_event = asyncio.Event()
        self._processing_events[umo] = proc_event

        task = asyncio.create_task(
            self._process_forward(event, forward_id, forward_payload, umo, is_group, should_reply, proc_event)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _process_forward(
        self,
        event: AstrMessageEvent,
        forward_id: str,
        forward_payload: Optional[Dict[str, Any]],
        umo: str,
        is_group: bool,
        should_reply: bool,
        proc_event: asyncio.Event,
    ):
        """处理合并转发消息"""
        try:
            logger.info(f"[SmartForward] 开始解析转发内容...")
            contexts, image_urls = await asyncio.wait_for(
                self._extract_forward_messages(
                    event, forward_id, forward_payload, current_depth=0
                ),
                timeout=30,
            )

            if not contexts:
                logger.info("[SmartForward] 转发内容为空，跳过")
                return

            logger.info(f"[SmartForward] 解析完成: {len(contexts)} 条消息, {len(image_urls)} 张图片")

            # 第一步：立即写入纯文本内容到历史（不等图片转述）
            # content 中已包含 [图片] 占位符，无需额外追加
            sender_name = event.get_sender_name()
            chat_type = "群聊" if is_group else "私聊"
            text_only_lines = []
            for ctx in contexts:
                line = f"{ctx['sender']}: {ctx['content']}"
                if ctx.get("has_video"):
                    line += " [视频]"
                text_only_lines.append(line)
            text_only_content = "\n".join(text_only_lines)
            text_only_message = f"[转发消息] {sender_name} 转发了一条{chat_type}消息，内容如下：\n{text_only_content}"
            await self._write_to_history(umo, text_only_message, "")
            logger.info("[SmartForward] 文本内容已写入历史")

            # 第二步：图片转述（并行处理）
            image_descriptions = []
            if image_urls:
                logger.info(f"[SmartForward] 开始图片转述 ({len(image_urls)} 张, 并行)...")
                image_descriptions = await asyncio.wait_for(
                    self._describe_images(image_urls),
                    timeout=120,
                )
                logger.info(f"[SmartForward] 图片转述完成")

            # 第三步：构建完整内容（含图片描述，严格按原始顺序替换占位符）
            chat_lines = []
            desc_idx = 0
            for ctx in contexts:
                line_content = ctx['content']
                img_count = ctx.get("image_count", 0)
                if img_count > 0:
                    for _ in range(img_count):
                        if desc_idx < len(image_descriptions):
                            # 将第 N 个 [图片] 占位符原地替换为描述
                            line_content = line_content.replace(
                                "[图片]", f"[图片: {image_descriptions[desc_idx]}]", 1
                            )
                            desc_idx += 1
                if ctx.get("has_video"):
                    line_content += " [视频]"
                chat_lines.append(f"{ctx['sender']}: {line_content}")

            chat_content = "\n".join(chat_lines)
            user_message_text = f"[转发消息] {sender_name} 转发了一条{chat_type}消息，内容如下：\n{chat_content}"

            # 更新历史：用含图片描述的完整内容替换之前的纯文本版本
            await self._update_last_history_entry(umo, text_only_message, user_message_text)

            # 存储结果并通知主管道可以继续
            # 仅当自己仍是当前活跃任务时才写入结果，避免过期任务覆盖新任务
            if self._processing_events.get(umo) is proc_event:
                self._forward_results[umo] = user_message_text
            proc_event.set()
            logger.info("[SmartForward] 转发处理完成，已通知主管道")

            if not should_reply:
                logger.info("[SmartForward] 完成（不回复，概率未触发）")
                return

            # 第四步：调用 LLM 生成回复
            group_id = event.get_group_id() or ""
            group_info = f"，来自群组 {group_id}" if is_group else ""

            # 截断保护
            max_content_len = 8000
            if len(chat_content) > max_content_len:
                chat_content_for_prompt = chat_content[:max_content_len] + "\n...(内容过长已截断)"
            else:
                chat_content_for_prompt = chat_content

            prompt = (
                f"你正在帮用户分析一条{chat_type}中的合并转发消息。"
                f"这条消息由 {sender_name} 转发{group_info}。"
                f"转发消息中包含以下聊天记录：\n\n"
                f"{chat_content_for_prompt}\n\n"
                f"请根据以上转发的聊天记录内容，给出自然、口语化的分析或总结。"
                f"像朋友之间聊天一样随意，不要加'根据聊天记录'之类的前缀。"
            )

            provider = self._get_provider(umo)
            if not provider:
                logger.error("[SmartForward] 未找到可用的LLM提供商")
                return

            logger.info(f"[SmartForward] 调用LLM分析 (prompt长度={len(prompt)})...")
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt),
                timeout=60,
            )

            if response and response.completion_text:
                reply_text = response.completion_text
                # 仅追加 assistant 回复（user 条目已在前面写入并更新）
                await self._append_assistant_reply(umo, reply_text)
                msg = MessageChain().message(reply_text)
                await self.context.send_message(umo, msg)
                logger.info(f"[SmartForward] 回复成功: {reply_text[:50]}...")
            else:
                logger.warning("[SmartForward] LLM返回无效响应，跳过回复")

        except asyncio.CancelledError:
            raise  # 保证取消语义正确传播（Python 3.8 兼容性）
        except asyncio.TimeoutError:
            logger.error("[SmartForward] 处理超时（解析/转述/LLM某一环节超时）")
            self._remove_dedup(forward_id, umo)
        except Exception as e:
            logger.error(f"[SmartForward] 处理转发消息失败: {type(e).__name__}: {e}")
            self._remove_dedup(forward_id, umo)
        finally:
            # 确保主管道不会永久阻塞
            if not proc_event.is_set():
                proc_event.set()
            # 仅清理自己注册的 event，避免误删后续 task 的 event
            if self._processing_events.get(umo) is proc_event:
                self._processing_events.pop(umo, None)
            # 兆底清理 _forward_results（防止主管道未触发钩子时泄漏）
            asyncio.get_running_loop().call_later(
                90, lambda: self._forward_results.pop(umo, None)
            )

    async def _write_to_history(self, umo: str, user_text: str, assistant_text: str):
        """将转发内容和LLM回复写入会话历史"""
        try:
            cm = getattr(self.context, "conversation_manager", None)
            if not cm:
                return

            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return

            conv = await cm.get_conversation(umo, cid, create_if_not_exists=True)
            if not conv:
                return

            msgs = []
            try:
                parsed = json.loads(conv.history) if getattr(conv, "history", "") else []
                if isinstance(parsed, list):
                    msgs = parsed
            except Exception:
                msgs = []

            msgs.append({"role": "user", "content": user_text})

            if assistant_text:
                msgs.append({"role": "assistant", "content": assistant_text})

            await cm.update_conversation(umo, cid, history=msgs)

        except Exception as e:
            logger.error(f"[SmartForward] 写入会话历史失败: {e}")

    async def _append_assistant_reply(self, umo: str, assistant_text: str):
        """仅追加 assistant 回复到历史（不重复追加 user 条目）"""
        try:
            cm = getattr(self.context, "conversation_manager", None)
            if not cm:
                return
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await cm.get_conversation(umo, cid, create_if_not_exists=False)
            if not conv:
                return
            msgs = []
            try:
                parsed = json.loads(conv.history) if getattr(conv, "history", "") else []
                if isinstance(parsed, list):
                    msgs = parsed
            except Exception:
                msgs = []
            msgs.append({"role": "assistant", "content": assistant_text})
            await cm.update_conversation(umo, cid, history=msgs)
        except Exception as e:
            logger.error(f"[SmartForward] 追加回复到历史失败: {e}")

    def _remove_dedup(self, forward_id: str, umo: str):
        """处理失败时移除去重标记，允许重试"""
        content = f"{forward_id}:{umo}"
        msg_hash = hashlib.md5(content.encode()).hexdigest()
        self._dedup_cache.pop(msg_hash, None)

    async def _update_last_history_entry(self, umo: str, old_text: str, new_text: str):
        """将历史中最后一条匹配的纯文本转发记录替换为含图片描述的完整版本"""
        try:
            cm = getattr(self.context, "conversation_manager", None)
            if not cm:
                return

            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return

            conv = await cm.get_conversation(umo, cid, create_if_not_exists=False)
            if not conv:
                return

            msgs = []
            try:
                parsed = json.loads(conv.history) if getattr(conv, "history", "") else []
                if isinstance(parsed, list):
                    msgs = parsed
            except Exception:
                return

            # 从后往前查找匹配的条目并替换
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "user" and msgs[i].get("content") == old_text:
                    msgs[i]["content"] = new_text
                    await cm.update_conversation(umo, cid, history=msgs)
                    logger.info("[SmartForward] 历史已更新为含图片描述的完整版本")
                    return

        except Exception as e:
            logger.warning(f"[SmartForward] 更新历史失败: {e}")

    async def _extract_forward_messages(
        self,
        event: AiocqhttpMessageEvent,
        forward_id: Optional[str],
        forward_payload: Optional[Dict[str, Any]],
        current_depth: int
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """提取合并转发中的消息内容"""
        if current_depth > self.max_nested_depth:
            return [], []

        messages: List[dict] = []

        if isinstance(forward_payload, dict):
            messages = self._extract_messages_from_forward_data(forward_payload)

        if not messages:
            if not forward_id:
                raise ValueError("缺少forward_id且无内联数据")

            client = event.bot
            try:
                forward_data = await client.api.call_action('get_forward_msg', id=forward_id)
                messages = self._extract_messages_from_forward_data(forward_data)
            except Exception as e:
                logger.error(f"[SmartForward] 获取合并转发失败: {e}")
                raise ValueError("无法获取合并转发内容")

        if not messages:
            raise ValueError("合并转发数据为空")

        contexts: List[Dict[str, Any]] = []
        image_urls: List[str] = []
        message_count = 0

        for node in messages:
            if message_count >= self.max_messages:
                break

            sender_obj = node.get("sender", {}) if isinstance(node, dict) else {}
            sender_name = (
                sender_obj.get("nickname")
                or node.get("nickname")
                or node.get("name")
                or "未知用户"
            ) if isinstance(node, dict) else "未知用户"

            content_chain = []
            if isinstance(node, dict):
                content_chain = (
                    node.get("content")
                    or node.get("message")
                    or node.get("raw_message")
                    or []
                )

            has_image = False
            has_video = False
            image_count = 0
            node_image_urls = []  # 本节点的图片 URL（绑定到该 context）
            text_parts = []

            if isinstance(content_chain, str):
                text_parts.append(content_chain)
            else:
                if isinstance(content_chain, dict):
                    content_chain = [content_chain]
                elif not isinstance(content_chain, list):
                    content_chain = [content_chain] if content_chain else []

                for segment in content_chain:
                    seg_type = None
                    seg_data = {}

                    if isinstance(segment, str):
                        text_parts.append(segment)
                        continue

                    if isinstance(segment, dict):
                        seg_type = segment.get("type")
                        seg_data = segment.get("data", {}) or {}
                    else:
                        seg_type = getattr(segment, "type", None)
                        seg_data = getattr(segment, "data", {}) or {}

                    if seg_type in ("text", "plain"):
                        text_parts.append(seg_data.get("text", ""))
                    elif seg_type == "image":
                        url = self._extract_image_url(seg_data)
                        if url:
                            has_image = True
                            image_count += 1
                            node_image_urls.append(url)
                            text_parts.append("[图片]")
                        else:
                            text_parts.append("[图片:链接缺失]")
                    elif seg_type == "video":
                        has_video = True
                        text_parts.append("[视频]")
                    elif seg_type == "file":
                        text_parts.append("[文件]")
                    elif seg_type == "forward" and self.parse_nested_forward:
                        nested_id = self._extract_forward_id(seg_data)
                        if nested_id and current_depth < self.max_nested_depth:
                            try:
                                nested_contexts, nested_images = await self._extract_forward_messages(
                                    event, nested_id, None, current_depth + 1
                                )
                                # 受 max_messages 限制
                                remaining = self.max_messages - message_count
                                if remaining > 0:
                                    kept = nested_contexts[:remaining]
                                    contexts.extend(kept)
                                    message_count += len(kept)
                            except Exception as e:
                                logger.warning(f"[SmartForward] 嵌套解析失败: {e}")

            content = "".join(text_parts).strip()
            if content or has_image or has_video:
                contexts.append({
                    "sender": sender_name,
                    "content": content,
                    "has_image": has_image,
                    "image_count": image_count,
                    "image_urls": node_image_urls,
                    "has_video": has_video
                })
                message_count += 1

        # 收集所有图片 URL（按 contexts 顺序扁平化，保证对齐）
        for ctx in contexts:
            image_urls.extend(ctx.get("image_urls", []))

        return contexts, image_urls

    def _nodes_to_raw_messages(self, nodes: list) -> List[dict]:
        """将 Comp.Nodes 中的 Node 组件转换为原始消息字典列表"""
        messages = []
        for node in nodes:
            name = getattr(node, "name", "") or "未知用户"
            uin = getattr(node, "uin", "") or ""
            content_chain = getattr(node, "content", []) or []

            # 将消息组件转换为 OneBot 格式的 dict 列表
            segments = []
            for comp in content_chain:
                comp_type = getattr(comp, "type", None)
                if comp_type is None:
                    continue
                type_str = comp_type.value if hasattr(comp_type, "value") else str(comp_type)
                type_str = type_str.lower()
                data = {}
                if type_str in ("plain", "text"):
                    data = {"text": getattr(comp, "text", "")}
                    type_str = "text"
                elif type_str == "image":
                    data = {
                        "url": getattr(comp, "url", "") or "",
                        "file": getattr(comp, "file", "") or "",
                    }
                elif type_str == "video":
                    data = {
                        "url": getattr(comp, "url", "") or "",
                        "file": getattr(comp, "file", "") or "",
                    }
                elif type_str == "forward":
                    data = {"id": getattr(comp, "id", "") or ""}
                else:
                    data = getattr(comp, "data", {}) or {}
                segments.append({"type": type_str, "data": data})

            messages.append({
                "sender": {"nickname": name, "user_id": uin},
                "nickname": name,
                "content": segments,
            })
        return messages

    def _extract_image_url(self, seg_data: dict) -> str:
        """从image段data中提取URL"""
        if not isinstance(seg_data, dict):
            return ""

        for key in ("url", "source_url", "src", "origin", "origin_url"):
            value = seg_data.get(key)
            if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
                return value.strip()

        file_value = seg_data.get("file")
        if isinstance(file_value, str) and file_value.strip().startswith(("http://", "https://")):
            return file_value.strip()

        return ""

    def _extract_forward_id(self, seg_data: dict) -> Optional[str]:
        """从forward段data中提取forward_id"""
        if not isinstance(seg_data, dict):
            return None

        for key in ("id", "resid", "forward_id"):
            value = seg_data.get(key)
            if value:
                return str(value)

        return None

    def _extract_messages_from_forward_data(self, forward_data: Any) -> List[dict]:
        """兼容不同协议端返回结构，提取messages列表"""
        if isinstance(forward_data, list):
            return [x for x in forward_data if isinstance(x, dict)]

        if not isinstance(forward_data, dict):
            return []

        messages = forward_data.get("messages")
        if isinstance(messages, list):
            return [x for x in messages if isinstance(x, dict)]

        message_list = forward_data.get("message")
        if isinstance(message_list, list):
            return [x for x in message_list if isinstance(x, dict)]

        data_obj = forward_data.get("data")
        if isinstance(data_obj, dict):
            messages = data_obj.get("messages")
            if isinstance(messages, list):
                return [x for x in messages if isinstance(x, dict)]

            message_list = data_obj.get("message")
            if isinstance(message_list, list):
                return [x for x in message_list if isinstance(x, dict)]

        return []

    async def terminate(self):
        """插件终止时资源释放"""
        # 取消所有后台任务
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        # 释放主管道等待（避免残留钩子阻塞后续消息）
        for evt in self._processing_events.values():
            evt.set()
        self._processing_events.clear()
        self._forward_results.clear()
        logger.info("[SmartForward] 插件已停止")
