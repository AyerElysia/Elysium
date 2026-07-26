# Default Chatter `send_text` 分条发送与节奏控制改造报告

日期：2026-03-26  
范围：仅修改 `default_chatter` 插件与其配置/测试，不涉及核心框架逻辑。

## 目标

将 `kokoro_flow_chatter`（KFC）中的两项能力迁移到 `default_chatter`（DFC）：

1. `send_text` 支持通过列表一次性分条发送（含 JSON 列表字符串兼容）。
2. 分条发送时按文本长度模拟“打字延迟”控制发送速度。

并在系统提示词中明确写出新 `send_text` 的调用规范，提升模型正确使用率。

## 实现内容

### 1) `send_text` 能力升级（核心）

文件：`plugins/default_chatter/plugin.py`

- `SendTextAction.execute` 的 `content` 入参由单字符串扩展为：
  - `str`
  - `list[str]`
- 新增分段解析逻辑（兼容三种输入）：
  - 普通字符串
  - 原生字符串数组
  - JSON 字符串数组（例如 `["A","B"]`）
- 分段发送行为：
  - 按顺序逐段发送
  - `reply_to` 仅首段使用，后续段不重复引用
  - 每段发送前会清洗可能泄漏的 `reason:` 元字段
- 增加段间延迟：
  - 第一段不延迟
  - 后续段根据字数和配置计算延迟后发送

### 2) 发送速度配置（插件配置层）

文件：`plugins/default_chatter/config.py`

在 `plugin` 下新增 `reply` 配置节：

- `typing_chars_per_sec`（默认 `15.0`）
- `typing_delay_min`（默认 `0.8`）
- `typing_delay_max`（默认 `4.0`）

同时给概率字段保持原状，不变更既有人格注入逻辑。

文件：`config/plugins/default_chatter/config.toml`

- 新增 `[plugin.reply]` 配置块，并写入默认值，确保可直接调参生效。

### 3) 系统提示词使用说明（按 KFC 风格）

文件：`plugins/default_chatter/plugin.py` 中 `system_prompt`

在“工具介绍”补充了 `send_text` 规范，关键包含：

1. 如果你需要发送多条消息，可以像这样分段`"content": ["你好", "请问你是谁？", "找我有什么事吗？"]`
2. 私聊场景下，`reply_to` 默认不要使用，除非确实需要引用某条历史消息来避免歧义。
3. `content` 只写发给用户的正文，不写 `reason/thought` 等元信息。

并同步更新了 `SendTextAction.action_description`，让工具 schema 与系统提示词一致。

## 测试与验证

新增测试文件：

- `test/plugins/test_default_chatter_send_text_action.py`
  - 普通字符串解析
  - JSON 列表字符串解析
  - 原生列表解析
  - `reason` 泄漏清洗
  - 延迟计算与上下限钳制

回归测试：

- `test/plugins/test_default_chatter_prompt_builder.py`

执行命令：

```bash
pytest -q -o addopts='' \
  /root/Elysia/Neo-MoFox/test/plugins/test_default_chatter_send_text_action.py \
  /root/Elysia/Neo-MoFox/test/plugins/test_default_chatter_prompt_builder.py
```

结果：`14 passed`

## 结果评估

- DFC 现在具备与 KFC 对齐的“分条发送 + 节奏控制”能力。
- 提示词与工具描述已明确引导模型优先使用列表分条，降低“连续多次调用 send_text”带来的冗余。
- 私聊 `reply_to` 已在提示词层明确“默认不用，必要才用”，符合当前策略要求。

## 备注

- 本次仅做插件侧实现，不改核心框架代码。
- 现有历史配置中若不设置 `[plugin.reply]`，代码会回退到内置默认值，不影响可用性。
