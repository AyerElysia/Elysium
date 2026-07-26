# Elysium 文档

## 具身体验（Minecraft）

- [自主性原则](./minecraft_autonomy_principles.md)
- [具身设计](./minecraft_embodiment_design.md)
- [快速上手](./minecraft_quick_start.md)
- [下一步计划](./minecraft_next_steps.md)
- [审查总结](./minecraft_review_summary.md)

## 数据与训练

- [训练数据湖](./training_data_lake.md)

## 意识对话

- [claude-fable-5 意识对话](./claude-fable-5-意识对话.md)

## 运行时基座（src/）

以下为实现层技术文档，记录插件运行时、LLM 调度、存储等基础设施：

- [app 层](./app/README.md)
- [core 层](./core/README.md)
- [kernel/llm](./llm/README.md)
- [kernel/db](./db/README.md)
- [kernel/event](./event/README.md)
- [kernel/concurrency](./concurrency/README.md)
- [kernel/config](./config/README.md)
- [kernel/logger](./logger/README.md)
- [scheduler](./scheduler/README.md)
- [storage](./storage/README.md)
- [vector_db](./vector_db/README.md)
- [guides](./guides/)
- [examples](./examples/)
# 文档总目录

本文件是 `docs` 目录的统一入口，用于快速导航到各层模块文档。

## 应用层（app）

- [app 总览](./app/README.md)
- [app 架构](./app/architecture.md)
- [app runtime](./app/runtime.md)
- [app plugin_system](./app/plugin_system.md)
- [app 命令手册](./app/command_reference.md)

## 领域层（core）

- [core 总览](./core/README.md)
- [core/components](./core/components/README.md)
- [core/config](./core/config/README.md)
- [core/managers](./core/managers/README.md)
- [core/models](./core/models/README.md)
- [core/prompt](./core/prompt/README.md)
- [core/transport](./core/transport/README.md)
- [core/utils](./core/utils/README.md)

## 内核层（kernel）

- [concurrency](./concurrency/README.md)
- [config](./config/README.md)
- [db](./db/README.md)
- [event](./event/README.md)
- [llm](./llm/README.md)
- [logger](./logger/README.md)
- [scheduler](./scheduler/README.md)
- [storage](./storage/README.md)
- [vector_db](./vector_db/README.md)

## 其他文档

- [examples](./examples/)
- [guides](./guides/)

## 建议维护规则

1. 新增模块文档时，先更新对应子目录 README，再更新本总目录。
2. 子目录入口建议统一命名为 README.md，降低跳转成本。
3. 对外公开的文档优先放在本目录可见路径下，避免深层隐藏。