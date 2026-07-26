# drive_core_plugin 实现报告

**日期：** 2026-03-22  
**范围：** `plugins/drive_core_plugin`  
**目标：** 落地“内驱力 / 自我引擎”最小骨架，让角色拥有按聊天隔离、可持久化、可自动推进的自我发问工作区。

---

## 1. 实现结果

`drive_core_plugin` 已完成基础落地，当前实现包含：

- 每个私聊/群聊独立的内驱力状态文件
- 自动生成“当前我想弄清什么”的自我课题
- 按固定对话数自动推进工作区
- 将当前内驱力状态注入主回复 prompt
- 提供命令查看、推进和重置状态
- 从 `diary_plugin`、`self_narrative_plugin`、`unfinished_thought_plugin` 汇总内部来源

---

## 2. 文件结构

- 插件入口：`plugins/drive_core_plugin/plugin.py`
- 配置：`plugins/drive_core_plugin/config.py`
- 服务：`plugins/drive_core_plugin/service.py`
- prompt：`plugins/drive_core_plugin/prompts.py`
- 命令：`plugins/drive_core_plugin/commands/drive_core_command.py`
- 事件处理器：
  - `plugins/drive_core_plugin/components/events/drive_core_scan_event.py`
  - `plugins/drive_core_plugin/components/events/drive_core_prompt_injector.py`
- 插件清单：`plugins/drive_core_plugin/manifest.json`
- 说明文档：`plugins/drive_core_plugin/README.md`

---

## 3. 核心行为

### 3.1 内驱力状态

每个聊天流独立维护一份状态，包含：

- 连续轴：`curiosity / initiative / affinity / withdrawal / fatigue / urgency / stability`
- 当前工作区：`topic / question / hypothesis / next_action / open_questions`
- 工具追踪：`tool_trace`
- 证据片段：`evidence`
- 历史任务记录：`history`

状态采用 JSON 持久化，默认路径：

- `data/drive_core/private/<stream_id>.json`
- `data/drive_core/group/<stream_id>.json`
- `data/drive_core/discuss/<stream_id>.json`

重启后可继续读取同一聊天流的状态。

### 3.2 自我发问流程

工作区不是由外部直接指定问题，而是由系统从内部材料中自己找问题。

当前实现遵循：

1. 先收集最近日记、连续记忆、自我叙事、未完成念头和最近对话
2. 再让模型生成当前课题、问题、假设和下一步
3. 如果模型不可用，则回退到基于内驱力轴的启发式工作区
4. 工作区达到上限后自动收束并进入历史记录

### 3.3 prompt 注入

在目标 `default_chatter_user_prompt` 构建时，自动注入当前内驱力块，内容包括：

- 主导倾向
- 当前问题
- 当前假设
- 下一步动作

默认只注入摘要，不强制暴露详细证据。

### 3.4 命令

支持：

- `/drive_core view`
- `/drive_core history`
- `/drive_core advance`
- `/drive_core reset`

---

## 4. 关键修正

本次实现过程中额外修复了几个容易导致“看似存在、实际失效”的问题：

- 修复 `DriveCorePlugin.get_components()` 里漏导入 `DriveCoreService` 的问题
- 修复共享人设提示词函数是异步函数，却被同步调用的问题
- 修复插件重载后可能继续持有旧服务单例的问题
- 在插件卸载时清空 `drive_core` 全局服务实例，避免热重载拿到旧上下文

---

## 5. 设计取舍

- 第一版没有引入复杂相关性打分，先用固定扫描 + 工作区推进跑通闭环
- 允许没有工具结果时回退到保守启发式，避免系统完全卡死
- 只注入摘要，不默认暴露全部内部历史，保持主回复模型的上下文稳定
- 共享人设提示词沿用 `diary_plugin` 的人设构建逻辑，尽量保证主观性一致

---

## 6. 验证结果

已完成：

- `python -m py_compile plugins/drive_core_plugin/plugin.py plugins/drive_core_plugin/service.py test/plugins/drive_core_plugin/test_drive_core_plugin.py`
- `pytest -q -o addopts='' test/plugins/drive_core_plugin/test_drive_core_plugin.py`

结果：

- `5 passed`

覆盖点包括：

1. 插件组件注册正常
2. 服务单例可重绑到新插件实例
3. 共享人设提示词正确 `await`
4. 固定对话数推进可持久化工作区
5. prompt 注入块可正确反映当前工作区

---

## 7. 风险与后续

- 真实联调用例还需要在运行环境里确认：
  - `on_chat_step` / `on_prompt_build` 的事件顺序是否完全符合预期
  - `diary`、`self_narrative`、`unfinished_thought` 的来源内容在真实会话里是否足够稳定
- 如果后续要继续增强“自我生活感”，下一步可以在这个骨架之上补：
  - 情绪底噪
  - 注意力预算
  - 未完成欲望池
  - 关系温度场

