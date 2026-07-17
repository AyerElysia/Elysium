# N.E.K.O 桌面前端桥接（提示词接管）

## 背景 / 架构边界

该桥接让 Neo-MoFox 为 N.E.K.O（Windows 上的 Live2D 桌面伴侣游戏/应用）构建
**主对话**请求，同时明确限制其复用范围：

- 复用 `LifeChatter._build_chat_router_prefix_prompt()` 生成的静态
  SOUL/USER/MEMORY 前缀，并读取 Neo-MoFox 的全局数据库聊天历史。
- 不进入完整 LifeChatter 全局 runtime，不包含其动态 suffix、消息调度和工具循环；
  因此这不是完整 runtime 的另一张等价前端。
- life_chatter 的 QQ/飞书工具不会注入 N.E.K.O 请求。N.E.K.O 的工具（Live2D
  动作、表情、TTS 等）会包装成 kernel 工具交给模型，桥接层只返回工具调用，
  不负责执行。包装层不主动改 schema，但 kernel/provider 仍可能按兼容性规则
  规范化 schema，不能保证到最终 provider 的请求字节级不变。

## 实现位置

- `plugins/live_bridge/neko_bridge.py`：纯函数/小对象，负责
  - 把 N.E.K.O 的 OpenAI 格式 `tools`（dict 列表）包装成 kernel LLM 模块要求的
    `LLMUsable` 协议对象（`NekoToolAdapter`）。适配层保留包装前的 schema，并
    避免因缺少通用参数入口而注入必填 `reason`；下游仍可做兼容性规范化。
  - 从 N.E.K.O 的 `messages` 数组提取当前用户发言，并将本轮尚未落盘的工具
    调用/结果尾巴转成文本摘要（`build_pending_tool_exchange_text`）。摘要使用逐条
    JSON 记录显式保留 `call id`、`name`、原始 `arguments` 与 `result` 的关联。
- `plugins/live_bridge/router/openai_router.py`：
  - `OpenAIRouter.chat_completions()` 路由：当 `request.model ==
    "elysia-neko"`（`_NEKO_MODEL_MARKER`）时，直接进入 `_handle_neko_chat`，
    优先于 STS2/Minecraft 的启发式检测。
  - `_handle_neko_chat` / `_generate_neko_reply`：直接调用 LLM（不走完整 Chatter
    调度），把 N.E.K.O 的 `tools` 注入同一次调用并同步读取 `tool_calls`；有工具
    时也会通过模型 `extra_params` 继续传递请求中的 `tool_choice`。
  - `_build_neko_system_prompt`：调用 `LifeChatter._build_chat_router_prefix_prompt`
    （SOUL + USER + MEMORY，**不带** `TOOLS.md`、动态 suffix 和 life_chatter 工具
    循环），再拼接 `_NEKO_OUTPUT_CONTRACT`。前缀有 60s（默认）缓存，且使用独立
    缓存槽位（`_neko_prefix_cache`）。
  - `_build_neko_user_prompt`：拼接全局数据库滚动聊天历史、本轮工具续接文本和
    当前用户发言。
  - `_neko_completion_response`：将 `LLMResponse.call_list` 转成 OpenAI
    `tool_calls` 格式返回给 N.E.K.O；没有工具调用时按普通文本回复处理。
  - 新 user 请求通过 `ON_MESSAGE_RECEIVED` 写入历史并设置
    `skip_chatter_distribution=True`。工具续接请求不会重复发布同一 user 消息；
    最终文本回复由 `StreamManager.add_sent_message_to_history()` 直接写入历史。

模型选择：默认使用 `model_tasks.actor`（与主对话相同档位的模型列表，而非
直播那种低成本快速回复模型），可用环境变量 `LIVE_BRIDGE_NEKO_MODEL_TASK`
覆盖。

## 已确认的技术事实

- N.E.K.O 的 `assist_api`（文字/工具通道）是完全可配置的 OpenAI 兼容接口，
  不限于枚举里的几个厂商。
- N.E.K.O 支持**按任务单独覆盖模型端点**（`ENABLE_CUSTOM_API` +
  `CONVERSATION_MODEL` / `CONVERSATION_MODEL_URL` / `CONVERSATION_MODEL_API_KEY`
  等字段，参见 `utils/config_manager.py: get_model_api_config`），且这些
  per-task 自定义字段默认值均为空字符串——只要不去动 `SUMMARY_MODEL_URL`
  等其他任务的自定义字段，打开 `ENABLE_CUSTOM_API` **只会**影响 `conversation`
  任务，不会影响 summary/correction/emotion/vision/agent 等 N.E.K.O 自己的
  内部工具调用。**这比"整体切换 assistApi 到自定义 provider"风险小得多**，
  是本次采用的方案。
- Neo-MoFox 的 OpenAI 兼容桥接服务监听在 `http_router_host:http_router_port`
  （见 `config/core.toml`，默认 `127.0.0.1:18000`），挂载路径 `/v1`。
- WSL2 默认是 NAT 模式，Windows 侧要稳定访问 WSL2 里的服务，建议在
  `C:\Users\<用户名>\.wslconfig` 加：
  ```ini
  [wsl2]
  networkingMode=mirrored
  ```
  然后重启 WSL（`wsl --shutdown`），这样 Windows 可以直接用 `127.0.0.1:18000`
  访问 Neo-MoFox，不用管 WSL2 那个每次重启都会变的临时 IP。

## Windows 侧配置示例（本任务未读取或修改）

如需启用桥接，只需编辑用户数据配置：
`<N.E.K.O 用户数据目录>\config\core_config.json`（不需要修改 Steam 游戏安装目录
下的 `api_providers.json`）：

```json
{
  "coreApi": "free",
  "assistApi": "free",
  "ENABLE_CUSTOM_API": true,
  "CONVERSATION_MODEL": "elysia-neko",
  "CONVERSATION_MODEL_URL": "http://127.0.0.1:18000/v1",
  "CONVERSATION_MODEL_API_KEY": "local-elysia"
}
```

- `CONVERSATION_MODEL` 的值必须是 `elysia-neko`（即 `openai_router.py` 里的
  `_NEKO_MODEL_MARKER`），路由靠这个值识别请求该走 NEKO 处理分支。
- `CONVERSATION_MODEL_URL` 如果 WSL2 开了 mirrored networking 用
  `127.0.0.1:18000/v1`；否则要换成当前 WSL2 的临时 IP（`ip addr show eth0`）。
- `CONVERSATION_MODEL_API_KEY` 随便填一个非空字符串即可，Neo-MoFox 这边的
  桥接不校验。
- `coreApi`/`assistApi` 保持原样（`free`），summary/correction/emotion/vision/
  agent 等任务不受影响，继续走原来的 doubao 代理。
- 改完需要重启 N.E.K.O（Steam 游戏进程）才会生效。

以上文件修改**尚未执行**——涉及修改 N.E.K.O 的运行时网络配置，按安全准则
需要用户明确确认后再写入，或者用户自己手动应用这段 JSON。

## 已知限制 / 未来可扩展点

- 当前 NEKO 侧只有单一固定身份（`stream_id="neko_desktop"`，
  `sender_id="neko_master"`），如果想让 NEKO 的"主人"和某个已有的 QQ/飞书
  联系人共享同一条记忆/关系状态，需要额外做身份映射，当前版本未做。
- 只接管了 `conversation`（主聊天）任务；N.E.K.O 的 `core_api`（实时语音，
  WebSocket）没有接管——用户尚未确认是否需要接管语音通道。
- 多步工具调用续接是靠把 `messages` 尾部的 assistant tool_calls / tool 结果
  转成一段文字塞进 user prompt（`<pending_tool_exchange>`），不是结构化的
  `ROLE.TOOL_RESULT` payload 回放；复杂的多轮工具编排链路可能需要进一步验证。
- 工具 schema 交给 kernel/provider 后可能被规范化；当前桥接不保证最终上游请求与
  N.E.K.O 输入字节级一致。
- 当前分支固定使用非流式 LLM 调用，尚未实现 OpenAI streaming 响应；请求中的
  `response_format` 也尚未转发或执行。
