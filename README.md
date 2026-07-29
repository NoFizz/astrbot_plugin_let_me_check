<h1 align="center">让我康康/astrbot_plugin_let_me_check</h1>

<p align="center">
  <img src="./logo.png" alt="让我康康 Logo" width="128" height="128">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-1.0.1-blue" alt="version">
  <img src="https://img.shields.io/badge/license-AGPL--3.0-green" alt="license">
  <img src="https://img.shields.io/badge/AstrBot->=4.26.0-orange" alt="AstrBot version">
  <img src="https://img.shields.io/badge/platform-aiocqhttp-lightgrey" alt="platform">
</p>

自动解析 QQ 合并转发消息（支持嵌套转发和图片转述），结合会话上下文调用 LLM 生成自然回复，支持群聊/私聊独立开关与白名单控制。

## 功能特性

- **合并转发解析**：自动检测并解析 QQ 合并转发消息，提取完整聊天记录
- **嵌套转发支持**：递归解析转发中嵌套的转发消息（最大深度可配置）
- **图片内容转述**：转发中的图片可调用多模态模型生成文字描述
- **LLM 智能回复**：将解析后的聊天记录交给 LLM 分析，生成自然口语化回复
- **会话上下文注入**：转发内容和 LLM 回复写入会话历史，后续对话可引用
- **群聊/私聊独立控制**：分别配置开关、白名单、回复概率
- **消息去重**：相同转发消息在同一会话中仅处理一次（1 小时有效期）

## 安装

### 方法一：通过插件市场安装（推荐）

1. 打开 AstrBot WebUI → 插件管理 → 插件市场。
2. 添加插件源（如尚未添加）：
   - 源名称：`AstrBot Official Plugin Market`
   - 源地址：`https://cloud-test.astrbot.app/api/v1/market/plugins.json`
3. 在插件市场中搜索 **让我康康**（`astrbot_plugin_let_me_check`），点击安装。
4. 等待安装完成，确认插件已启用。

### 方法二：从 GitHub 安装

1. 打开 AstrBot WebUI → 插件管理 → 新增插件。
2. 选择 **从 GitHub 安装**。
3. 填入仓库地址：
   ```
   https://github.com/NoFizz/astrbot_plugin_let_me_check
   ```
4. 等待安装完成，确认插件已启用。

### 方法三：手动安装

1. 将本仓库克隆或下载到 AstrBot 的插件目录：
   ```bash
   cd AstrBot/data/plugins
   git clone https://github.com/NoFizz/astrbot_plugin_let_me_check.git
   ```
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
| 回复概率 | float | 1.0 | 0.0~1.0，控制收到转发后是否回复 |

### 解析设置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 最大处理消息数 | int | 200 | 单次分析的最大聊天记录条数 |
| 解析嵌套转发 | bool | true | 是否递归解析嵌套的合并转发 |
| 最大嵌套深度 | int | 3 | 嵌套解析的最大递归层数 |

### 模型配置

| 配置项 | 说明 |
|--------|------|
| 对话模型提供商 | 留空使用 AstrBot 默认模型，可选择指定提供商 |
| 图片转述模型提供商 | 留空使用 AstrBot 全局图片转述模型，用于描述转发中的图片 |
| 图片转述提示词 | 自定义发送给图片转述模型的提示词，留空使用默认提示 |

## 使用示例

本插件无用户命令，采用事件驱动方式自动工作。

当群聊或私聊中收到 QQ 合并转发消息时，插件会自动：
1. 检测并解析转发内容（包括嵌套转发和图片）
2. 将解析结果交给 LLM 分析
3. 生成自然口语化回复并发送

回复概率设为 0 时，转发内容仍会写入会话历史（供后续对话引用），但不会主动回复。

## 支持平台

仅支持 **aiocqhttp**（OneBot v11）。

原因：转发消息的解析依赖协议端的 `get_forward_msg` API，该 API 为 OneBot v11 特有。其他平台收到消息会自动跳过。

## 数据存储与隐私

- **内存去重缓存**：使用 MD5 哈希记录已处理的转发消息，1 小时有效期，不持久化到磁盘。
- **会话历史**：解析后的转发内容和 LLM 回复会写入 AstrBot 会话历史，供后续对话引用。
- 本插件不调用任何外部第三方 API（LLM 调用走 AstrBot 已配置的模型提供商）。

## 工作原理

```
收到消息
  → 检测合并转发消息段（Comp.Forward）
  → 去重校验（MD5 哈希，1h 有效期）
  → 异步解析转发内容（调用 get_forward_msg API）
  → 递归处理嵌套转发
  → 图片调用多模态模型转述（可选）
  → 构建 Prompt → 调用 LLM 生成回复
  → 写入会话历史 → 发送回复
```

## 故障排查

- 转发消息未被解析：确认协议端支持 `get_forward_msg` API（NapCat、Lagrange 等均支持）
- 图片转述不生效：确认配置了支持图片输入的模型提供商
- 其他平台无效：本插件仅支持 aiocqhttp，其他平台会自动跳过

## 许可证

本项目基于 [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html) 许可证开源。

## 作者

**NoFizz** · [GitHub](https://github.com/NoFizz)

如遇问题或有功能建议，欢迎提交 [Issue](https://github.com/NoFizz/astrbot_plugin_let_me_check/issues)。
