"""Schema compliance guard tests.

校验 _conf_schema.json 与代码常量 / 预期默认值保持同步，
防止 WebUI 配置与代码逻辑悄然分叉。
"""

import json
from pathlib import Path

from astrbot_plugin_let_me_check.models import DEFAULT_IMAGE_CAPTION_PROMPT

# tests/ 的上一级目录即插件根目录（与 CWD 无关）。
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PLUGIN_ROOT / "_conf_schema.json"


def _load_schema() -> dict:
    """读取 _conf_schema.json（UTF-8）。"""
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_caption_prompt_default_matches_code_constant():
    """schema 的 image_caption_prompt.default 必须与 models.py 常量一致。"""
    schema = _load_schema()
    default = schema["model_config"]["items"]["image_caption_prompt"]["default"]
    assert default == DEFAULT_IMAGE_CAPTION_PROMPT


def test_caption_concurrency_default_is_five():
    """schema 的 image_caption_concurrency.default 必须为 5。"""
    schema = _load_schema()
    default = schema["model_config"]["items"]["image_caption_concurrency"]["default"]
    assert default == 5
