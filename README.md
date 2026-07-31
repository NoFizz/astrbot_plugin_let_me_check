<h1 align="center">astrbot_plugin_let_me_check</h1>

<p align="center">
  <img src="./logo.png" alt="让我康康 Logo" width="128" height="128">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-2.0.1-blue" alt="version">
  <img src="https://img.shields.io/badge/license-AGPL--3.0-green" alt="license">
  <img src="https://img.shields.io/badge/python-3.10+-blue" alt="python">
  <img src="https://img.shields.io/badge/AstrBot->=4.26.0-orange" alt="AstrBot version">
</p>

自动解析 QQ 合并转发消息（支持嵌套转发和图片转述），结合会话上下文调用 LLM 生成自然回复，支持群聊/私聊独立开关与白名单控制。

## 功能特性

- **合并转发解析**：自动检测并解析 QQ 合并转发消息，提取完整聊天记录
- **嵌套转发支持**：并行递归解析转发中嵌套的转发消息（最大深度可配置）
- **图片内容转述**：转发中的图片调用多模态模型生成文字描述（URL 去重，避免重复转述）
- **双回复模式**：注入主管道（由 Bot 统一回复）或主动发送独立回复
- **会话上下文注入**：转发内容和 LLM 回复原子化写入会话历史，后续对话可引用
- **群聊/私聊独立控制**：分别配置开关与白名单
- **消息去重**：相同转发消息在同一会话中仅处理一次（1 小时有效期）
- **并发保护**：同一会话同时只处理一条转发，避免状态冲突

## 安装

### 方法一：通过 AstrBot WebUI 安装（推荐）

1. 打开 AstrBot WebUI → 插件管理 → 新增插件。
2. 选择 **从 GitHub 安装**。
3. 填入仓库地址：
   ```
   https://github.com/NoFizz/astrbot_plugin_let_me_check
   ```
4. 等待安装完成，确认插件已启用。

### 方法二：手动安装

1. 将本仓库克隆或下载到 AstrBot 的插件目录：
   ```bash
   git clone https://github.com/NoFizz/astrbot_plugin_let_me_check.git
   ```
   或将 ZIP 解压到 `AstrBot/data/plugins/` 目录下。
2. 在 AstrBot WebUI 中重载插件，或重启 AstrBot。

### 安装后检查

- 在 WebUI 插件管理中确认插件状态为"已启用"且无报错。
- 本插件无第三方依赖，无需额外安装。

## 配置说明

在 AstrBot WebUI 插件管理中点击本插件进行配置，所有配置项均有悬浮提示。

### 群聊 / 私聊

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 启用 | bool | true | 是否对该类型会话启用转发解析 |
| 启用白名单 | bool | false | 开启后仅白名单中的会话生效 |
| 白名单 | list | [] | UMO 列表，格式如 `aiocqhttp:GroupMessage:123456` |

### 解析设置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 最大处理消息数 | int | 200 | 单次分析的最大聊天记录条数 |
| 解析嵌套转发 | bool | true | 是否递归解析嵌套的合并转发 |
| 最大嵌套深度 | int | 3 | 嵌套解析的最大递归层数 |

### 模型配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 对话模型提供商 | string | 空 | 留空使用当前配置文件中的"默认对话模型" |
| 图片转述模型提供商 | string | 空 | 留空依次使用"默认图片转述模型"→"群聊图片转述模型"→"默认对话模型" |
| 图片转述提示词 | text | 请用中文简短描述这张图片的内容。 | 发送给图片转述模型的提示词 |
| 回复模式 | string | inject_only | `inject_only`（注入主管道，推荐）或 `proactive_only`（主动回复） |
| 图片转述并发数 | int | 5 | 同时调用图片转述模型的最大并发数 |

### 回复模式说明

| 模式 | 行为 |
|------|------|
| `inject_only`（推荐） | 收到转发后解析并暂存内容，用户 @机器人 时由主管道结合上下文统一回复 |
| `proactive_only` | 收到转发后立即独立调用 LLM 生成回复并主动发送 |

## 使用示例

本插件无用户命令，采用事件驱动方式自动工作。

**inject_only 模式（默认）**：
1. 群聊中有人发送合并转发消息
2. 插件自动解析内容（含图片转述），写入会话历史
3. 用户 @机器人 并提问（如"如何评价"）
4. 主管道 LLM 结合注入的转发内容生成回复

**proactive_only 模式**：
1. 群聊中有人发送合并转发消息
2. 插件自动解析内容并立即调用 LLM 分析
3. 生成自然口语化回复并主动发送

## 支持平台

仅支持 **aiocqhttp**（OneBot v11）。

原因：转发消息的解析依赖协议端的 `get_forward_msg` API，该 API 为 OneBot v11 特有。其他平台收到消息会自动跳过。

## 数据存储与隐私

- **内存去重缓存**：使用 MD5 哈希记录已处理的转发消息，1 小时有效期，不持久化到磁盘。
- **会话历史**：解析后的转发内容和 LLM 回复会写入 AstrBot 会话历史，供后续对话引用。
- 本插件不调用任何外部第三方 API（LLM 调用走 AstrBot 已配置的模型提供商）。

## 故障排查

| 问题 | 可能原因 | 解决方法 |
|------|----------|----------|
| 转发消息未被解析 | 协议端不支持 `get_forward_msg` API | 使用 NapCat、Lagrange 等支持的协议端 |
| 图片转述不生效 | 未配置可用的多模态模型 | 在 AstrBot 配置中设置"默认图片转述模型"或插件内指定 |
| 其他平台无效 | 仅支持 aiocqhttp | 其他平台会自动跳过，属正常行为 |
| inject_only 模式不回复 | 该模式需用户 @机器人 触发 | 改用 proactive_only 模式实现自动回复 |

## 许可证

本项目基于 [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html) 许可证开源。

## 作者

**NoFizz** · [GitHub](https://github.com/NoFizz)

如遇问题或有功能建议，欢迎提交 [Issue](https://github.com/NoFizz/astrbot_plugin_let_me_check/issues)。
