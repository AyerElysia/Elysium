# evolving_personality 插件化落地方案

## 1. 背景与目标

目标是将 `/root/Elysia/evolving_personality` 的 JPAF 能力（人格权重、短期补偿、长期反思）落地为 Neo-MoFox 的一个可长期运行插件，而不是实验脚本。

本方案强调三点：

- 符合当前框架插件规范（`BasePlugin + manifest + components + config`）
- 复用框架既有能力（`llm_api`、`model.toml`、事件总线、prompt 注入）
- 保留 JPAF 核心机制，同时去掉实验代码中的不安全与不可运维部分

---

## 2. 可行性结论

结论：**可行，且适配成本中等**。

原因：

1. Neo-MoFox 已提供人格类插件所需关键能力：  
   - 事件触发（`ON_CHATTER_STEP_RESULT`）  
   - Prompt 注入（`on_prompt_build`）  
   - 按 `stream_id` 状态隔离  
   - 标准 LLM 请求封装与重试策略  
2. JPAF 的核心算法可拆成“服务层纯逻辑 + 事件触发 + 注入输出”的形态。
3. 参考 `drive_core_plugin` / `self_narrative_plugin` 已有成熟模板，可直接复用工程结构。

不建议直接搬运原项目，必须重构后再接入。

---

## 3. 现状差距分析（必须改造项）

### 3.1 工程结构差距

- 现状：脚本入口（`change_test.py`、`personality_test.py`）+ 本地文件 I/O + 线程并发。
- 目标：插件组件化（Service/EventHandler/Command/Config），由框架生命周期托管。

### 3.2 LLM 调用差距

- 现状：`llm_link.py` 直接使用 OpenAI SDK 和 `para.env`。
- 目标：统一改为 `src.app.plugin_system.api.llm_api` + `config/model.toml` 任务路由。

### 3.3 安全与健壮性差距

- 现状：存在 `eval` 解析回复、无上限 `while` 重试。
- 目标：严格 JSON 解析、有限重试、失败降级、可观测日志。

### 3.4 运行语义差距

- 现状：离线实验逻辑（批量问卷/场景测评）与在线对话运行逻辑耦合。
- 目标：把“实验评估能力”与“在线人格更新能力”解耦，插件只保留在线核心。

---

## 4. 插件定位与边界

插件名建议：`personality_engine_plugin`（或 `jpaf_personality_plugin`）。

### 4.1 插件职责

- 维护每个聊天流的人格状态（MBTI + 八功能权重 + 变更历史）
- 按对话推进触发人格微调（短期补偿）
- 在关键阈值下触发反思决策（主辅重排/重构）
- 向系统 prompt 注入“当前人格态摘要”

### 4.2 非职责（避免职责膨胀）

- 不做平台接入（Adapter）
- 不替代主对话器（Chatter）
- 不负责长期记忆存储总线（交由现有记忆插件）
- 不内置批量实验流水线（可做独立开发脚本或测试模块）

---

## 5. 组件设计（对齐 AI 插件编写规范）

## 5.1 Plugin

- `plugin.py`
- `@register_plugin`
- `plugin_name = "personality_engine_plugin"`
- `configs = [PersonalityEngineConfig]`
- `get_components()` 返回以下组件类

## 5.2 Config

- `config.py`，路径：`config/plugins/personality_engine_plugin/config.toml`
- 建议配置节：
  - `[plugin]`：`enabled`, `inject_prompt`
  - `[storage]`：`base_path`, `max_history_records`
  - `[scan]`：`trigger_every_n_messages`, `max_context_messages`
  - `[model]`：`task_name`, `fallback_task_name`
  - `[personality]`：`default_mbti`, `change_weight`, `max_retry_parse`
  - `[prompt]`：`target_prompt_names`, `prompt_title`, `inject_detail_level`

## 5.3 Service（核心）

- `service.py`，`service_name = "personality_engine_service"`
- 对外方法建议：
  - `get_state(stream_id, chat_type, ...)`
  - `observe_chat_turn(...)`
  - `advance_personality_step(...)`
  - `render_prompt_block(...)`
  - `render_state_summary(...)`
  - `reset_state(...)`

状态存储建议：

- `data/personality_engine/private/<stream_id>.json`
- `data/personality_engine/group/<stream_id>.json`
- `data/personality_engine/discuss/<stream_id>.json`

## 5.4 EventHandler（推进）

- `components/events/personality_scan_event.py`
- 订阅：`EventType.ON_CHATTER_STEP_RESULT`
- 职责：累计消息计数，到阈值时调用 `service.advance_personality_step()`

## 5.5 EventHandler（注入）

- `components/events/personality_prompt_injector.py`
- 订阅：`on_prompt_build`
- 职责：对目标模板名（默认 `default_chatter_system_prompt`）追加人格摘要

## 5.6 Command

- `commands/personality_command.py`
- 路由建议：
  - `/personality view`
  - `/personality advance`
  - `/personality reset`
  - `/personality set_mbti INTJ`

---

## 6. 数据模型设计

建议最小可用状态结构（每流一份）：

```json
{
  "version": 1,
  "stream_id": "xxx",
  "chat_type": "private",
  "updated_at": "2026-03-24T00:00:00+08:00",
  "message_count_since_scan": 0,
  "mbti": "INTJ",
  "weights": {
    "Ti": 0.05,
    "Te": 0.22,
    "Fi": 0.05,
    "Fe": 0.05,
    "Ni": 0.48,
    "Ne": 0.05,
    "Si": 0.05,
    "Se": 0.05
  },
  "change_history": {
    "Ti": 0.0,
    "Te": 0.0,
    "Fi": 0.0,
    "Fe": 0.0,
    "Ni": 0.0,
    "Ne": 0.0,
    "Si": 0.0,
    "Se": 0.0
  },
  "current_hypothesis": "",
  "last_selected_function": "",
  "history": []
}
```

---

## 7. 算法迁移策略（JPAF -> 在线插件）

## 7.1 保留

- `mbti_to_function` / `function_to_mbti` 映射
- `change_weight` 微增逻辑
- 四类反思分支（主辅互换、主变更、辅变更、双重重构）
- 权重归一化与阈值约束

## 7.2 重写

- `extract_dict_content`：改为 JSON-only 解析，禁止 `eval`
- 所有“格式错重试”：改为上限重试（如 2-3 次）+ fallback
- `llm_link.py`：改为框架 `LLMRequest`
- 历史上下文来源：改为从 `chat_stream.context` 读取，而非脚本本地变量

## 7.3 失败降级策略

- LLM 解析失败：本轮不改 MBTI，仅衰减 `change_history`
- LLM 超时：记录错误并回退到轻量规则更新
- 数据损坏：自动重建默认状态并保留损坏文件备份

---

## 8. 与现有插件的协同

建议读但不强依赖以下服务：

- `diary_plugin:service:diary_service`（近期事件摘要）
- `self_narrative_plugin:service:self_narrative_service`（稳定边界信息）
- `unfinished_thought_plugin:service:unfinished_thought_service`（未收束问题）

协同原则：

- 可用则增强，不可用不阻塞人格引擎运行
- 只读依赖，不在人格插件内修改他插件状态

---

## 9. Prompt 注入规范

注入内容应保持“短而可执行”，避免写成大段心理学论文。

建议注入块：

- 当前 MBTI 与主辅功能
- 当前最活跃补偿功能
- 本轮行为倾向提示（如“更偏结构化/更偏探索式”）
- 一条约束提醒（避免人设漂移）

示例：

```text
【人格态】
- 当前类型：INTJ（Ni-Te）
- 当前补偿：Si（短期上升）
- 回应倾向：先抽象归纳，再给可执行步骤
- 稳定约束：保持一致性，不为了迎合而自相矛盾
```

---

## 10. 实施计划（4 个里程碑）

## M1：插件骨架与状态存储

- 新建插件目录、`manifest.json`、`plugin.py`、`config.py`
- 实现 Service 的状态读写、按流隔离、基本命令
- 验收：`/personality view/reset` 可用，状态文件可落盘

## M2：在线更新链路

- 接入 `ON_CHATTER_STEP_RESULT` 触发链
- 接入 `llm_api` 模型任务路由
- 完成功能选择 + `change_history` 更新 + 归一化
- 验收：对话推进后权重发生可控变化

## M3：反思机制与注入

- 实现四类反思分支逻辑（含阈值与边界约束）
- 接入 `on_prompt_build` 注入人格块
- 验收：可观察到 MBTI/主辅在阈值场景下变化，prompt 注入稳定

## M4：稳定性与测试

- 替换 `eval`、增加重试上限、超时降级
- 单元测试 + 集成测试 + 回归测试
- 验收：异常场景不阻断主聊天流程

---

## 11. 测试与验收标准

### 11.1 单元测试

- 权重更新函数：输入场景 -> 输出权重变化符合预期
- 反思分支：阈值边界条件全覆盖
- 解析器：非法 JSON 不崩溃，返回可控错误

### 11.2 集成测试

- 事件触发后状态更新
- prompt 注入仅在目标模板生效
- `task_name` 不可用时 fallback 正常

### 11.3 验收门槛

1. 插件可加载/卸载，无残留异常日志。  
2. 连续 100 轮模拟对话，无死循环与阻塞。  
3. 关闭插件时主聊天行为无回归。  
4. 打开插件时人格注入可观察且不破坏原 prompt 结构。  

---

## 12. 风险与应对

- 风险：人格漂移过快，影响角色稳定。  
  - 应对：限制单轮最大变更、增加冷却、引入衰减。  

- 风险：LLM 输出格式不稳定导致更新失败。  
  - 应对：JSON schema 校验 + 有限重试 + fallback。  

- 风险：插件间循环耦合。  
  - 应对：只读依赖、禁止跨插件写操作。  

- 风险：Prompt 注入过重拉高 token 成本。  
  - 应对：注入块长度上限 + 精简字段 + 配置开关。  

---

## 13. 交付物清单

- `plugins/personality_engine_plugin/manifest.json`
- `plugins/personality_engine_plugin/plugin.py`
- `plugins/personality_engine_plugin/config.py`
- `plugins/personality_engine_plugin/service.py`
- `plugins/personality_engine_plugin/components/events/personality_scan_event.py`
- `plugins/personality_engine_plugin/components/events/personality_prompt_injector.py`
- `plugins/personality_engine_plugin/commands/personality_command.py`
- `config/plugins/personality_engine_plugin/config.toml`
- `test/plugins/personality_engine_plugin/*`
- `report/personality_engine_plugin_implementation_report.md`

---

## 14. 推荐下一步

先按 M1 + M2 做一个最小可跑版本，不先做复杂反思分支。  
跑通后再逐步引入 M3 的完整人格重构逻辑，避免一次性大改导致排障困难。

