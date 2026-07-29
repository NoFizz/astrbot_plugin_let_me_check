"""
让我康康 - 类型化数据模型

定义插件内部流转的数据结构，所有模块共享这些类型。
"""

from dataclasses import dataclass, field
from enum import Enum


class ForwardSource(Enum):
    """转发消息的检测来源"""

    COMPONENT = "component"  # Comp.Forward 标准组件
    NODES = "nodes"  # Comp.Nodes 组件
    RAW = "raw"  # 原始消息中的 forward 段


@dataclass
class ParsedMessage:
    """解析后的单条转发消息"""

    sender: str
    content: str
    has_image: bool = False
    image_count: int = 0
    image_urls: list[str] = field(default_factory=list)
    has_video: bool = False


@dataclass
class ForwardDetectResult:
    """转发消息检测结果"""

    forward_id: str | None
    forward_payload: dict | None
    source: ForwardSource


@dataclass
class ProcessingResult:
    """转发消息提取的最终结果"""

    messages: list[ParsedMessage]
    image_urls: list[str]  # 按消息顺序扁平化，保证与占位符对齐
