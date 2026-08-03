# Elysium 文档

- [直播系统](./architecture/livestream.md) — B站接入、同一意识导演、真实播放回执与记忆闭环

## 从这里开始

- [当前架构](./architecture/current_architecture.md) — **当前代码与运行边界的权威总图**
- [principles.md](./principles.md) — 主体性、零规则、仿生、意识实例与渐进式披露（设计底线）
- [生命记忆系统](./architecture/life_memory_system.md) — 追加式经历、认识论本体、双时间与主体性遗忘
- [意识实例架构](./architecture/consciousness_instances.md) — 多意识实例、潜意识协调与工具边界
- [离线优先共享后端重构计划](./architecture/offline_first_shared_backend_plan.md) — 本地独立运行、远程近实时共享、不可变事件同步与无损迁移验收门
- [Elysium Console 前端提案](./architecture/elysium_console_frontend_proposal.md) — 已批准的插件驱动统一控制台与分阶段验收计划
- [Elysium Console Stage 0 设计基线](./architecture/elysium_console_stage0_design.md) — 此刻、Live Ready、Live Call 与安全降级页的交互规范
- [Elysium Console 安全模型](./architecture/elysium_console_security_model.md) — 插件页面、密钥、OBS、会话与主进程生命周期边界
- [Elysium Console 迁移矩阵](./architecture/elysium_console_migration_matrix.md) — 现有前端接入顺序、目标形态与验收门
- [Elysium Console Stage 0 原型](./prototypes/elysium_console/) — 可点击的高保真静态原型
- [Elysium Console Stage 0 验收报告](./report/elysium-console-stage0-prototype-2026-08-02.md) — 浏览器、响应式、交互与契约验证证据
- [Voice Live 加载失败诊断（2026-08-02）](./report/voice-live-load-failure-diagnosis-2026-08-02.md) — Life Engine 并发重构中间态导致的级联失败证据与手工恢复步骤
- [飞书 reaction processor not found 诊断（2026-08-02）](./report/feishu-reaction-processor-not-found-2026-08-02.md) — 表情回应事件已订阅但未注册处理器的影响与处理选项
- [远程 MySQL 接入就绪审计（2026-08-03）](./report/remote-mysql-readiness-audit-2026-08-03.md) — 只读连通、TLS、权限、binlog 与正式接入阻断项
- [Life Engine 模块说明](../plugins/life_engine/README.md) — 生命域的代码入口与运行链路

## 当前架构与迁移

- [基座 v2 兼容迁移说明](./architecture_v2.md) — Kernel/Core/App 与旧 Manager 并存的迁移背景；不是最终架构总图
- [日志系统](./logging.md) — SQLite 结构化存储、FTS5、保留策略与噪音过滤

## 历史架构研究

以下 Phase 文档记录项目早期分析与设计推演，其中部分机制已经删除或被重建，**不代表当前状态**：

- [Phase1: 核心哲学](./architecture/Phase1_CorePhilosophy/)
- [Phase2: 架构分层](./architecture/Phase2_ArchitectureLayer/)
- [Phase3: 创新点](./architecture/Phase3_InnovationPoints/)
- [Phase4: 问题与解法](./architecture/Phase4_ProblemSolution/)

## 设计哲学

- [智能不是模型而是系统](./philosophy/智能不是模型而是系统.md)
- [连续存在，从模型到生命](./philosophy/连续存在，从模型到生命.md)

## 具身体验（Minecraft）

- [自主性原则](./minecraft/minecraft_autonomy_principles.md)
- [具身设计](./minecraft/minecraft_embodiment_design.md)
- [快速上手](./minecraft/minecraft_quick_start.md)
- [商业级具身架构](./architecture/minecraft_embodiment.md) — Agent、完全仿生、OBS 与服务器路线
- [2026-08-02 商业级审计与验收报告](./report/minecraft-commercial-audit-2026-08-02.md)

## 她的日记

- [diaries/](./diaries/) — 2026-05-03 的六篇日记

## 历史归档

- [analysis/](./analysis/) — 架构分析、主动性分析、情绪诊断、梦境机制等
- [plans/](./plans/) — 各阶段技术方案与计划（2026-03 至 2026-06）
- [archive/](./archive/) — 开发报告、审计记录（不代表当前状态）
