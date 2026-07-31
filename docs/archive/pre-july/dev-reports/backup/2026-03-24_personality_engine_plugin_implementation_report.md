# 2026-03-24 personality_engine_plugin 实现报告（第三版）

## 1. 任务目标

在不改动核心代码的前提下，将 `evolving_personality` 能力插件化接入 Neo-MoFox，形成可加载、可运行、可观测、可测试的人格引擎插件，并将注入方式从“状态标签”升级为更接近原论文的“机制注入”。

## 2. 实现范围

本次仅改动插件自身、插件配置、插件测试与报告文档，未改动 `src/` 核心框架代码。

新增插件目录：

- `plugins/personality_engine_plugin/`

新增核心文件：

- `plugins/personality_engine_plugin/manifest.json`
- `plugins/personality_engine_plugin/plugin.py`
- `plugins/personality_engine_plugin/config.py`
- `plugins/personality_engine_plugin/service.py`
- `plugins/personality_engine_plugin/prompts.py`
- `plugins/personality_engine_plugin/commands/personality_command.py`
- `plugins/personality_engine_plugin/components/events/personality_scan_event.py`
- `plugins/personality_engine_plugin/components/events/personality_prompt_injector.py`

新增配置文件：

- `config/plugins/personality_engine_plugin/config.toml`

新增测试：

- `test/plugins/personality_engine_plugin/test_personality_engine_service.py`
- `test/plugins/personality_engine_plugin/test_personality_prompt_injector.py`
- `test/plugins/personality_engine_plugin/test_personality_scan_event.py`

本版增量改动：

- `plugins/personality_engine_plugin/prompts.py`（论文式机制注入模板）
- `plugins/personality_engine_plugin/service.py`（基线补偿/假设、聊天类型兜底、注入增强）
- `plugins/personality_engine_plugin/service.py`（新增 LLM 反思判定链）
- `plugins/personality_engine_plugin/components/events/personality_scan_event.py`（事件参数兜底）
- `plugins/personality_engine_plugin/components/events/personality_prompt_injector.py`（chat_type 空值兜底）
- `plugins/personality_engine_plugin/config.py`（新增 `prompt.mode` 等配置）
- `config/plugins/personality_engine_plugin/config.toml`（默认切到 `paper_strict`）
- `plugins/personality_engine_plugin/README.md`

## 3. 功能说明

### 3.1 状态管理

- 按 `stream_id + chat_type` 隔离人格状态。
- 存储内容包含：`mbti`、八功能权重、`change_history`、最后补偿功能、当前假设、结构变更历史。
- 存储路径：`data/personality_engine/{private|group|discuss}/<stream_id>.json`。

### 3.2 自动推进

- 事件订阅：`ON_CHATTER_STEP_RESULT`。
- 每累计 `trigger_every_n_messages` 次有效对话触发一次人格推进。
- 推进流程：
  1. 收集最近消息窗口；
  2. 通过 LLM（可关闭）选择本轮补偿功能；
  3. 失败时启发式回退；
  4. 应用权重变化与四类结构反思；
  5. 写回状态。

### 3.3 Prompt 注入

- 事件订阅：`on_prompt_build`。
- 默认注入目标：`default_chatter_system_prompt`。
- 注入字段：`extra_info`（若是 user prompt 则注入 `extra`）。
- 注入模式：
  - `compact`：轻量摘要注入。
  - `paper_strict`：机制型注入（补偿机制、执行步骤、八功能映射、当前结构、可选历史变化与权重）。

`paper_strict` 注入文案已按原论文 `Personality_changes/prompt.py` 的组织方式进行对齐：

- `Compensation Mechanism`
- `Following the steps`
- `Psychological Type Characteristics`

同时保留工程化约束：主回复不强制 JSON，以避免破坏聊天体验。

### 3.4 反思机制（与原论文预期对齐）

本版新增“LLM 反思判定”链路：

1. 当触发反思阈值后，先判断反思动作类型：
   - 主辅互换（swap_main_aux）
   - 仅主导变化（change_main）
   - 仅辅助变化（change_aux）
   - 主辅重构（reorganize_main_aux）
2. 针对动作类型调用 LLM 反思提示词，要求输出：
   - `judgment`（yes/no）
   - `reason`
   - 对应权重字段（如 `main_weight`、`ori_main_weight` 等）
3. 若 LLM 输出可解析且有效，则按输出执行结构变更并归一化；
4. 若 LLM 不可用/输出非法，则自动回退到规则反思。

该链路对应了原论文实现中“反思阶段再次调用 LLM 判断结构变更”的预期行为。

### 3.5 命令接口

- `/personality view`
- `/personality advance`
- `/personality reset`
- `/personality set_mbti <MBTI>`

### 3.6 “长期暂无”修复

针对“聊天很久仍显示 当前补偿/当前假设=暂无”的问题，本版做了三层修复：

1. 新状态初始化即写入基线值：  
   `last_selected_function = 主导功能`，`current_hypothesis = 基线人格假设`。
2. Prompt 注入读状态时增加聊天类型兜底：  
   避免 `chat_type` 不一致导致读到空状态。
3. 扫描事件增加 `stream_id/chat_type` 兜底提取：  
   减少因事件参数形态差异导致推进链被跳过。

## 4. 稳定性处理

已实现以下防故障措施：

- 禁止 `eval`，仅使用 JSON 解析（含安全剪裁解析）。
- LLM 输出解析失败采用有限重试，不无限循环。
- LLM 不可用时自动启发式回退，主流程不中断。
- 反思 LLM 不可用时自动回退规则反思，结构演化不中断。
- 权重与变更历史均做清洗和归一化，避免非法值扩散。
- 注入模式切换异常时回退 `compact`，避免因配置值错误导致注入失败。

## 5. 测试结果

执行命令：

```bash
pytest -q -o addopts='' \
  test/plugins/personality_engine_plugin/test_personality_engine_service.py \
  test/plugins/personality_engine_plugin/test_personality_prompt_injector.py \
  test/plugins/personality_engine_plugin/test_personality_scan_event.py
```

结果：

- `4 passed`

补充检查：

- 新插件 Python 文件已通过 `py_compile`。
- 新插件模块导入通过。

## 6. 与原论文对齐度说明

已对齐部分：

- 八功能语义映射 + 补偿机制 + 分步决策框架注入到 system prompt。
- 保留“补偿功能选择 -> 权重变化 -> 结构反思”主链。
- 反思阶段支持 LLM 二次判定（yes/no + 权重），并应用结构变化。

有意保留的工程化差异：

- 原论文实验脚本将部分环节输出为严格 JSON；插件在线对话链路中不强制主回复为 JSON，避免破坏正常聊天输出。

## 7. 已知边界

- 当前版本将“功能选择”交给 LLM（可配置关闭），但“结构反思决策”采用规则引擎以优先保证稳定性。
- 未实现离线批量问卷实验链路（`Personality_test`），仅实现在线人格演化插件链路。

## 8. 后续建议

- 增加更多场景化回归测试（长对话稳定性、不同 MBTI 初始值切换）。
- 若后续需要更强可解释性，可在变更历史中记录更多中间阈值数据。
- 可考虑新增 `/personality history` 命令便于线上观测演化轨迹。
