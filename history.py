"""
让我康康 - 会话历史管理

负责将转发内容和 LLM 回复原子化写入会话历史。
使用 conversation_manager.add_message_pair() 替代手动 JSON 操作，
消除竞态条件和脆弱的字符串匹配更新。
"""

import json

from astrbot.api import logger


class HistoryManager:
    """会话历史管理器，提供原子化写入"""

    def __init__(self, context):
        self._context = context

    async def write_forward_pair(
        self, umo: str, user_text: str, assistant_text: str = ""
    ) -> None:
        """将转发内容（user）和 LLM 回复（assistant）一次性写入会话历史。

        优先使用 add_message_pair() 原子 API（AstrBot >= 4.5.7），
        回退到 update_conversation() 手动写入。

        Args:
            umo: 统一消息来源标识
            user_text: 转发消息的完整文本（含图片描述）
            assistant_text: LLM 回复文本，为空则仅写入 user 条目
        """
        try:
            cm = getattr(self._context, "conversation_manager", None)
            if not cm:
                return

            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return

            # 优先使用原子 API
            if hasattr(cm, "add_message_pair") and assistant_text:
                await self._write_via_add_message_pair(
                    cm, cid, user_text, assistant_text
                )
            else:
                await self._write_via_update_conversation(
                    cm, umo, cid, user_text, assistant_text
                )

        except Exception as e:
            logger.error(f"[SmartForward] 写入会话历史失败: {type(e).__name__}: {e}")

    async def _write_via_add_message_pair(
        self, cm, cid: str, user_text: str, assistant_text: str
    ) -> None:
        """使用 add_message_pair 原子写入（推荐路径）"""
        try:
            from astrbot.core.agent.message import (
                AssistantMessageSegment,
                TextPart,
                UserMessageSegment,
            )

            user_msg = UserMessageSegment(content=[TextPart(text=user_text)])
            assistant_msg = AssistantMessageSegment(
                content=[TextPart(text=assistant_text)]
            )
            await cm.add_message_pair(
                cid=cid, user_message=user_msg, assistant_message=assistant_msg
            )
        except ImportError:
            # 消息类型不可用时回退
            logger.warning(
                "[SmartForward] agent.message 不可用，回退到 update_conversation"
            )
            umo = getattr(cm, "_current_umo", "")
            await self._write_via_update_conversation(
                cm, umo, cid, user_text, assistant_text
            )

    async def _write_via_update_conversation(
        self, cm, umo: str, cid: str, user_text: str, assistant_text: str
    ) -> None:
        """回退路径：手动 JSON 操作写入历史"""
        conv = await cm.get_conversation(umo, cid, create_if_not_exists=True)
        if not conv:
            return

        msgs = []
        try:
            history = getattr(conv, "history", "")
            if isinstance(history, list):
                msgs = history
            elif isinstance(history, str) and history:
                parsed = json.loads(history)
                if isinstance(parsed, list):
                    msgs = parsed
        except Exception:
            msgs = []

        msgs.append({"role": "user", "content": user_text})
        if assistant_text:
            msgs.append({"role": "assistant", "content": assistant_text})

        await cm.update_conversation(umo, cid, history=msgs)
