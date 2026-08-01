<h1>astrbot_plugin_let_me_check/让我康康</h1>

<p align="center">
  <img src="./logo.png" alt="让我康康 Logo" width="128" height="128" style="vertical-align: middle">
  <img src="https://count.getloli.com/@astrbot_plugin_let_me_check?theme=moebooru" alt="Moe Counter" style="vertical-align: middle">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-2.1.0-blue?style=flat" alt="version">
  <img src="https://img.shields.io/badge/license-AGPL--3.0-green?style=flat" alt="license">
  <img src="https://img.shields.io/badge/python-3.10+-blue?style=flat" alt="python">
  <img src="https://img.shields.io/badge/AstrBot->=4.26.0-orange?style=flat" alt="AstrBot version">
</p>

自动解析 QQ 合并转发消息（支持嵌套转发和图片转述），结合会话上下文调用 LLM 生成自然回复，支持群聊/私聊独立开关与白名单控制。

## 功能特性

- **合并转发解析**：自动检测 QQ 合并转发消息，用户触发 LLM 时统一解析，提取完整聊天记录
- **嵌套转发支持**：并行递归解析转发中嵌套的转发消息（最大深度可配置）
- **图片内容转述**：转发中的图片调用多模态模型生成文字描述（URL 去重，避免重复转述）
- **按需解析（默认零 Token）**：收到转发仅暂存，用户触发 LLM 回复时统一解析注入
- **会话上下文注入**：转发内容解析后写入会话历史，后续对话可引用
- **群聊/私聊独立控制**：分别配置开关与白名单
- **消息去重**：相同转发消息在同一会话中仅处理一次（1 小时有效期）
- **暂存队列**：每会话暂存上限 10 条（超限丢最旧），暂存有效期 24 小时

## 安装

### 方法一：通过插件源安装（推荐）

1. 打开 AstrBot WebUI → 插件管理 → 插件市场。
2. 添加插件源（如尚未添加）：
   - 源名称：`AstrBot Official Plugin Market`
   - 源地址：`https://cloud-test.astrbot.app/api/v1/market/plugins.json`
3. 在插件市场中搜索 **让我康康**（`astrbot_plugin_let_me_check`），点击安装。
4. 等待安装完成，确认插件已启用。

### 方法二：通过 AstrBot WebUI 从 GitHub 安装

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
| 启用图片转述 | bool | true | 关闭后图片仅显示为占位符，不调用任何模型 |
| 图片转述并发数 | int | 5 | 同时调用图片转述模型的最大并发数 |

## 使用示例

本插件无用户命令，采用事件驱动方式自动工作。

**按需解析（默认）**：
1. 群聊中有人发送合并转发消息 → 插件仅暂存，不解析、不消耗 Token
2. 用户 @机器人 并提问（如"如何评价"）→ 主管道触发 LLM 请求
3. 插件解析暂存的全部转发（含图片转述，按入队顺序合并），注入请求
4. 主管道 LLM 结合转发内容生成回复，转发内容写入会话历史

**私聊直发纯转发**：纯转发消息本身不触发 LLM（已吞掉该次调用）；用户后续发送任意消息时，自动带上该转发一并解析。

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
| 发送转发后机器人不回复 | 按需解析模式下纯转发不触发 LLM，属正常行为 | 在转发后继续发送一条消息（如 @机器人 提问）触发解析 |

## 许可证

本项目基于 [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html) 许可证开源。

## 作者

**NoFizz** · [GitHub](https://github.com/NoFizz)

如遇问题或有功能建议，欢迎提交 [Issue](https://github.com/NoFizz/astrbot_plugin_let_me_check/issues)。
