"""
让我康康 - 转发消息检测与提取

负责：
- 检测消息链中的合并转发组件（Comp.Forward / Comp.Nodes / 原始 forward 段）
- 调用协议端 API 获取转发内容
- 递归解析嵌套转发
- 提取结构化消息列表

纯逻辑模块，无副作用，可独立测试。
"""

import asyncio
import hashlib
import json
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger

from .models import (
    FILE_PLACEHOLDER,
    IMAGE_NO_URL_PLACEHOLDER,
    IMAGE_PLACEHOLDER,
    VIDEO_PLACEHOLDER,
    ForwardDetectResult,
    ForwardSource,
    ParsedMessage,
    ProcessingResult,
)

# 单次 get_forward_msg 协议调用超时（秒），与 main.py 外层 _EXTRACT_TIMEOUT 分层
_GET_FORWARD_TIMEOUT = 30
# 嵌套转发并行获取并发上限，防止对协议端造成瞬时压力
_NESTED_FETCH_CONCURRENCY = 8


def detect_forward(event) -> ForwardDetectResult | None:
    """检测消息链中的合并转发组件，返回检测结果或 None。

    检测顺序：
    1. Comp.Forward 标准组件
    2. Comp.Nodes 组件（部分协议端）
    3. 原始消息中的 forward 段（兜底）
    """
    forward_id: str | None = None
    forward_payload: dict | None = None
    source = ForwardSource.COMPONENT

    try:
        # 1. 遍历消息链检测标准组件
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Forward):
                seg_data = getattr(seg, "data", {}) or {}
                forward_id = (
                    getattr(seg, "id", None)
                    or getattr(seg, "resid", None)
                    or seg_data.get("id")
                    or seg_data.get("resid")
                    or seg_data.get("forward_id")
                )
                if isinstance(seg_data, dict) and isinstance(
                    seg_data.get("messages"), list
                ):
                    forward_payload = {"messages": seg_data["messages"]}
                if forward_id:
                    forward_id = str(forward_id)
                source = ForwardSource.COMPONENT
                break

            elif isinstance(seg, Comp.Nodes):
                nodes = getattr(seg, "nodes", [])
                if nodes:
                    forward_payload = {"messages": _nodes_to_raw_messages(nodes)}
                    source = ForwardSource.NODES
                    break

        # 2. 兜底：从原始消息中查找 forward 段
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
                            source = ForwardSource.RAW
                            logger.info(
                                f"[SmartForward] 从原始消息检测到 forward 段 | type={rtype} | id={forward_id}"
                            )
                            break

    except Exception as e:
        logger.error(f"[SmartForward] 转发检测异常: {type(e).__name__}: {e}")
        return None

    if not forward_id and not forward_payload:
        return None

    # 无 forward_id 但有内联数据时，生成合成 ID
    if not forward_id and forward_payload:
        raw = json.dumps(forward_payload, ensure_ascii=False, sort_keys=True)
        forward_id = f"inline_{hashlib.md5(raw.encode()).hexdigest()[:16]}"

    return ForwardDetectResult(
        forward_id=forward_id, forward_payload=forward_payload, source=source
    )


async def extract_messages(
    event,
    detect_result: ForwardDetectResult,
    max_messages: int = 200,
    parse_nested: bool = True,
    max_depth: int = 3,
) -> ProcessingResult:
    """提取合并转发中的消息内容。

    Args:
        event: AiocqhttpMessageEvent 实例（用于调用协议端 API）
        detect_result: detect_forward 的返回值
        max_messages: 最大处理消息数
        parse_nested: 是否解析嵌套转发
        max_depth: 最大嵌套深度

    Returns:
        ProcessingResult 包含结构化消息列表和扁平化图片 URL 列表
    """
    messages = await _resolve_raw_messages(event, detect_result)
    if not messages:
        return ProcessingResult(messages=[], image_urls=[])

    contexts, image_urls = await _parse_nodes(
        event, messages, max_messages, parse_nested, max_depth, current_depth=0
    )
    return ProcessingResult(messages=contexts, image_urls=image_urls)


# ─── 内部辅助函数 ───────────────────────────────────────────────


async def _resolve_raw_messages(
    event, detect_result: ForwardDetectResult
) -> list[dict]:
    """获取原始消息字典列表（从内联数据或协议端 API）"""
    if isinstance(detect_result.forward_payload, dict):
        messages = _extract_messages_from_forward_data(detect_result.forward_payload)
        if messages:
            return messages

    if not detect_result.forward_id:
        return []

    client = event.bot
    try:
        forward_data = await asyncio.wait_for(
            client.api.call_action("get_forward_msg", id=detect_result.forward_id),
            timeout=_GET_FORWARD_TIMEOUT,
        )
        return _extract_messages_from_forward_data(forward_data)
    except Exception as e:
        logger.error(f"[SmartForward] 获取合并转发失败: {e}")
        return []


async def _parse_nodes(
    event,
    messages: list[dict],
    max_messages: int,
    parse_nested: bool,
    max_depth: int,
    current_depth: int,
    _semaphore: asyncio.Semaphore | None = None,
) -> tuple[list[ParsedMessage], list[str]]:
    """解析消息节点列表，返回 (ParsedMessage 列表, 扁平化图片 URL 列表)"""
    if current_depth > max_depth:
        return [], []

    contexts: list[ParsedMessage] = []
    image_urls: list[str] = []
    message_count = 0

    # 第一遍：收集需要并行解析的嵌套转发
    nested_tasks: list[tuple[int, str]] = []  # (插入位置, nested_id)

    for node in messages:
        if message_count >= max_messages:
            break

        sender_name = _extract_sender_name(node)
        content_chain = _extract_content_chain(node)
        text_parts: list[str] = []
        has_video = False
        image_count = 0
        node_image_urls: list[str] = []

        if isinstance(content_chain, str):
            text_parts.append(content_chain)
        else:
            if isinstance(content_chain, dict):
                content_chain = [content_chain]
            elif not isinstance(content_chain, list):
                content_chain = [content_chain] if content_chain else []

            for segment in content_chain:
                seg_type, seg_data = _parse_segment(segment)

                if seg_type is None and isinstance(segment, str):
                    text_parts.append(segment)
                elif seg_type in ("text", "plain"):
                    text_parts.append(seg_data.get("text", ""))
                elif seg_type == "image":
                    url = _extract_image_url(seg_data)
                    if url:
                        image_count += 1
                        node_image_urls.append(url)
                        text_parts.append(IMAGE_PLACEHOLDER)
                    else:
                        text_parts.append(IMAGE_NO_URL_PLACEHOLDER)
                elif seg_type == "video":
                    has_video = True
                    text_parts.append(VIDEO_PLACEHOLDER)
                elif seg_type == "file":
                    text_parts.append(FILE_PLACEHOLDER)
                elif (
                    seg_type == "forward" and parse_nested and current_depth < max_depth
                ):
                    nested_id = _extract_forward_id(seg_data)
                    if nested_id:
                        nested_tasks.append((len(contexts), nested_id))

        content = "".join(text_parts).strip()
        if content or image_count > 0 or has_video:
            contexts.append(
                ParsedMessage(
                    sender=sender_name,
                    content=content,
                    image_count=image_count,
                    image_urls=node_image_urls,
                    has_video=has_video,
                )
            )
            message_count += 1

    # 第二遍：并行解析嵌套转发并插入对应位置
    if nested_tasks:
        remaining = max_messages - message_count
        if remaining > 0:
            # 整棵解析树共享同一信号量，全局约束并发协议获取次数
            if _semaphore is None:
                _semaphore = asyncio.Semaphore(_NESTED_FETCH_CONCURRENCY)
            # 并行获取所有嵌套转发
            nested_results = await asyncio.gather(
                *[
                    _resolve_nested(
                        event,
                        nid,
                        max_messages,
                        parse_nested,
                        max_depth,
                        current_depth + 1,
                        _semaphore,
                    )
                    for _, nid in nested_tasks
                ],
                return_exceptions=True,
            )
            # 从后向前插入，避免索引偏移
            offset = 0
            for i, (insert_pos, _) in enumerate(nested_tasks):
                result = nested_results[i]
                if isinstance(result, Exception):
                    logger.warning(f"[SmartForward] 嵌套解析失败: {result}")
                    continue
                nested_contexts, nested_images = result
                if nested_contexts:
                    kept = nested_contexts[: remaining - offset]
                    if kept:
                        pos = insert_pos + offset
                        for j, ctx in enumerate(kept):
                            contexts.insert(pos + j, ctx)
                        offset += len(kept)

    # 收集所有图片 URL（按 contexts 顺序扁平化）
    for ctx in contexts:
        image_urls.extend(ctx.image_urls)

    return contexts, image_urls


async def _resolve_nested(
    event,
    nested_id: str,
    max_messages: int,
    parse_nested: bool,
    max_depth: int,
    depth: int,
    _semaphore: asyncio.Semaphore,
) -> tuple[list[ParsedMessage], list[str]]:
    """解析单个嵌套转发"""
    nested_detect = ForwardDetectResult(
        forward_id=nested_id, forward_payload=None, source=ForwardSource.COMPONENT
    )
    async with _semaphore:
        raw_msgs = await _resolve_raw_messages(event, nested_detect)
    if not raw_msgs:
        return [], []
    return await _parse_nodes(
        event, raw_msgs, max_messages, parse_nested, max_depth, depth, _semaphore
    )


def _extract_sender_name(node) -> str:
    """从节点中提取发送者名称"""
    if not isinstance(node, dict):
        return "未知用户"
    sender_obj = node.get("sender", {}) if isinstance(node.get("sender"), dict) else {}
    return (
        sender_obj.get("nickname")
        or node.get("nickname")
        or node.get("name")
        or "未知用户"
    )


def _extract_content_chain(node) -> Any:
    """从节点中提取消息链"""
    if not isinstance(node, dict):
        return []
    return node.get("content") or node.get("message") or node.get("raw_message") or []


def _parse_segment(segment) -> tuple[str | None, dict]:
    """解析单个消息段，返回 (type, data)"""
    if isinstance(segment, str):
        return None, {}
    if isinstance(segment, dict):
        return segment.get("type"), segment.get("data", {}) or {}
    return getattr(segment, "type", None), getattr(segment, "data", {}) or {}


def _extract_image_url(seg_data: dict) -> str:
    """从 image 段 data 中提取 URL"""
    if not isinstance(seg_data, dict):
        return ""
    for key in ("url", "source_url", "src", "origin", "origin_url"):
        value = seg_data.get(key)
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            return value.strip()
    file_value = seg_data.get("file")
    if isinstance(file_value, str) and file_value.strip().startswith(
        ("http://", "https://")
    ):
        return file_value.strip()
    return ""


def _extract_forward_id(seg_data: dict) -> str | None:
    """从 forward 段 data 中提取 forward_id"""
    if not isinstance(seg_data, dict):
        return None
    for key in ("id", "resid", "forward_id"):
        value = seg_data.get(key)
        if value:
            return str(value)
    return None


def _nodes_to_raw_messages(nodes: list) -> list[dict]:
    """将 Comp.Nodes 中的 Node 组件转换为原始消息字典列表"""
    messages = []
    for node in nodes:
        name = getattr(node, "name", "") or "未知用户"
        uin = getattr(node, "uin", "") or ""
        content_chain = getattr(node, "content", []) or []

        segments = []
        for comp in content_chain:
            comp_type = getattr(comp, "type", None)
            if comp_type is None:
                continue
            type_str = (
                comp_type.value if hasattr(comp_type, "value") else str(comp_type)
            )
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

        messages.append(
            {
                "sender": {"nickname": name, "user_id": uin},
                "nickname": name,
                "content": segments,
            }
        )
    return messages


def _extract_messages_from_forward_data(forward_data: Any) -> list[dict]:
    """兼容不同协议端返回结构，提取 messages 列表"""
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
