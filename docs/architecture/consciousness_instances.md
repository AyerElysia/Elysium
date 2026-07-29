# 意识实例架构

> 2026-07-28 确立。从"单一 chatter 单例"演进到"多意识实例 + 潜意识协调"。

## 核心范式

```
旧：LifeChatter 单例 + 全局工具注册 + chatter_allow 过滤
新：ConsciousnessInstance + 类型工具清单 + 实例级上下文
```

## 架构层次

```
潜意识（life_engine 心跳）
  ├── WorldState：结构化世界模型（关系/话题/身体/场景）
  ├── ConsciousnessRegistry：意识实例注册表
  ├── 心跳协调：同步多意识场景，检测跨意识关联
  └── nucleus 工具：不受意识实例影响

意识实例（ConsciousnessInstance）
  ├── chat_global：日常对话（私聊、群聊）
  ├── minecraft：具身交互（纯视觉→键鼠）
  ├── livestream：直播互动（弹幕）
  ├── memory_witness：第一人称见证意识（安静记录自己，把经历落成不可变账本；无对外工具，专注见证与记录）
  └── 每个实例有：
      ├── 独立滚动上下文 (runtime/consciousness/{id}/)
      ├── 独立工具清单 (tool_manifests.py)
      └── 独立感知过滤 (PerceptionFilter)
```

## 关键文件

| 文件 | 职责 |
|------|------|
| `service/consciousness.py` | 意识实例模型 + 注册表 |
| `service/world_state.py` | 结构化世界模型（多意识共享） |
| `service/tool_manifests.py` | 意识类型工具清单 |
| `core/chatter.py` | 意识实例引擎（LifeChatter） |
| `service/subconscious_context.py` | 潜意识上下文管理 |

## 工具编排

每种意识类型声明自己需要的工具集：

- **chat**：send_text, pass_and_wait, think, report_state, inner_dialogue, inner_query, fetch_chat_history, grep_events, send_emoji_meme
- **minecraft**：nucleus_minecraft, send_text, think, report_state
- **livestream**：send_text, think, report_state, inner_query, fetch_chat_history

清单是建议性的——她可以通过 skill 系统的渐进式披露使用清单外的能力。

## 跨意识感知

意识实例之间不直接读取彼此的滚动上下文（隔离）。跨意识信息通过：

1. **WorldState.active_scenes**：摘要级场景状态
2. **nucleus_tell_dfc**：潜意识紧急推送信息差
3. **action-report_state**：主意识主动报告状态变化

## 数据迁移

滚动上下文路径从 `runtime/life_chatter_rolling_context.json` 迁移到 `runtime/consciousness/{instance_id}/rolling_context.json`。首次启动时自动迁移，不丢数据。
