"""
让我康康 - 会话历史管理

负责将转发内容和 LLM 回复写入会话历史。
通过 conversation_manager.update_conversation() 写入 user/assistant 条目。
"""

import asyncio
import json

from astrbot.api import logger


class HistoryManager:
    """会话历史管理器：串行化本插件实例内的历史写入。

    通过 asyncio.Lock 保护"读取-追加-整体覆写"（read-modify-write）流程，
    避免同一插件实例内并发调用 write_forward_pair() 时相互覆盖、丢失条目。

    注意：该锁仅覆盖本插件实例自身的写入。AstrBot 框架对同一会话的并发写入
    不在保护范围内 —— 框架没有原子追加接口（add_message_pair 内部同样执行
    整体覆写式的读写修改），因此本类不承诺跨实例的绝对原子性。
    """

    def __init__(self, context):
        self._context = context
        self._lock = asyncio.Lock()

    async def write_forward_pair(
        self, umo: str, user_text: str, assistant_text: str = ""
    ) -> None:
        """将转发内容（user）和 LLM 回复（assistant）一次性写入会话历史。

        通过 update_conversation() 写入 user/assistant 条目。

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

            async with self._lock:
                await self._write_via_update_conversation(
                    cm, umo, cid, user_text, assistant_text
                )

        except Exception as e:
            logger.error(f"[SmartForward] 写入会话历史失败: {type(e).__name__}: {e}")

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
        except Exception as e:
            logger.warning(
                f"[SmartForward] 解析会话历史失败，使用空历史继续: {type(e).__name__}: {e}"
            )
            msgs = []

        msgs.append({"role": "user", "content": user_text})
        if assistant_text:
            msgs.append({"role": "assistant", "content": assistant_text})

        await cm.update_conversation(umo, cid, history=msgs)
