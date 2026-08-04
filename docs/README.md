# Elysium 文档

- [主体上下文投影](./architecture/主体上下文投影.md) — SOUL/USER/MEMORY 共同权威、按 surface/预算派生、revision 固定与不可变恢复契约

- [直播系统](./architecture/直播系统.md) — B站接入、同一意识导演、真实播放回执与记忆闭环

## 从这里开始

- [当前架构](./architecture/Elysium当前架构.md) — **当前代码与运行边界的权威总图**
- [principles.md](./principles.md) — 主体性、零规则、仿生、意识实例与渐进式披露（设计底线）
- [生命记忆系统](./architecture/生命记忆系统.md) — 追加式经历、认识论本体、双时间与主体性遗忘
- [统一记忆归档架构](./architecture/Elysium统一记忆归档架构.md) — 逐行/逐字节可校验灾备、归档角色与投影边界
- [记忆跨节点共享契约](./architecture/Elysium记忆跨节点共享契约.md) — 同一主体的事件复制、开放记忆谱系、冲突与投影重建验收门
- [自主意向执行协议](./architecture/生命引擎自主意向执行协议.md) — occurrence 身份、有限租约、stream 所有权、回执幂等与崩溃恢复
- [意识实例架构](./architecture/意识实例架构.md) — 多意识实例、潜意识协调与工具边界
- [离线优先共享后端重构计划](./architecture/Elysium离线优先共享后端重构计划.md) — 本地独立运行、远程近实时共享、不可变事件同步与无损迁移验收门
- [离线同步内核](./architecture/Elysium离线同步内核.md) — 阶段二已实现的 Outbox/Inbox、幂等账本、游标、冲突、退避与隐私边界
- [离线同步运行手册](./operations/offline_sync_runbook.md) — 手动启用、健康检查、断网恢复、降级回退与备份核对
- [MySQL 核心持久化边界](./architecture/MySQL核心持久化边界.md) — Core 关系数据、生命域本地权威库、方言契约与人工切换边界
- [MySQL 迁移、备份与恢复手册](./operations/mysql_migration_and_backup.md) — 审计、快照、原子迁移、连续备份与恢复演练
- [生命域可选 MySQL / 本地存储重构方案](./architecture/Elysium生命域可选MySQL与本地存储重构方案.md) — 单写权威、双后端合同、无损迁移与分阶段完成定义
- [生命域存储快照与权威切换运行手册](./operations/life_storage_backend_runbook.md) — 候选快照、generation、epoch/fencing、切换门与回滚边界
- [生命域可选存储阶段 3 / Life Event 交付报告](./report/life-storage-phase3-life-event-2026-08-04.md) — 事件账本双适配、候选复制控制、86,094 条真实影子复制与 0 差异反向导出
- [生命域可选存储阶段 4A / 主体文档交付报告](./report/life-storage-phase4-subject-document-2026-08-04.md) — 精确字节版本、CAS/投影安全、1,404 份真实 MySQL shadow 与 0 差异反向导出
- [生命域可选存储阶段 2B / Memory 迁移报告](./report/life-storage-phase2b-memory-migration-2026-08-04.md) — 32 张显式表、210,104 条真实记录、删除历史保真与三方同根反向恢复
- [生命域可选存储阶段 2C / Presence、World 与 Subject 接线报告](./report/life-storage-phase2c-presence-world-subject-integration-2026-08-04.md) — 2,031 条真实领域记录三方同根恢复、单 runtime 注入与主体写前入账
- [Elysium Console 前端提案](./architecture/Elysium控制台前端提案.md) — 已批准的插件驱动统一控制台与分阶段验收计划
- [Elysium Console Stage 0 设计基线](./architecture/Elysium控制台阶段零交互设计.md) — 此刻、Live Ready、Live Call 与安全降级页的交互规范
- [Elysium Console 安全模型](./architecture/Elysium控制台安全模型.md) — 插件页面、密钥、OBS、会话与主进程生命周期边界
- [Elysium Console 迁移矩阵](./architecture/Elysium控制台页面迁移矩阵.md) — 现有前端接入顺序、目标形态与验收门
- [Elysium Console Stage 0 原型](./prototypes/elysium_console/) — 可点击的高保真静态原型
- [Elysium Console Stage 0 验收报告](./report/elysium-console-stage0-prototype-2026-08-02.md) — 浏览器、响应式、交互与契约验证证据
- [Voice Live 加载失败诊断（2026-08-02）](./report/voice-live-load-failure-diagnosis-2026-08-02.md) — Life Engine 并发重构中间态导致的级联失败证据与手工恢复步骤
- [飞书 reaction processor not found 诊断（2026-08-02）](./report/feishu-reaction-processor-not-found-2026-08-02.md) — 表情回应事件已订阅但未注册处理器的影响与处理选项
- [NapCat / QQNT 掉线恢复与真实链路验证（2026-08-04）](./report/napcat-qqnt-recovery-validation-2026-08-04.md) — QQNT 版本回退、复合健康判断、真实私聊收发证据与人工回滚边界
- [远程 MySQL 接入就绪审计（2026-08-03）](./report/remote-mysql-readiness-audit-2026-08-03.md) — 只读连通、TLS、权限、binlog 与正式接入阻断项
- [MySQL 基座迁移与恢复验证（2026-08-03）](./report/mysql-foundation-validation-2026-08-03.md) — 真实数据迁移、两次安全拦截、逐表指纹、备份恢复与限制
- [生命域可选存储阶段 0/1 交付（2026-08-04）](./report/life-storage-phase0-phase1-2026-08-04.md) — 六个真实 SQLite 源、精确文件资产、存储内核、远程 MySQL 控制面与未切换声明
- [离线同步阶段二验证（2026-08-03）](./report/offline-sync-phase2-validation-2026-08-03.md) — 本地/远端 MySQL 全链路、并发幂等、崩溃恢复、Inbox 游标与回归证据
- [记忆归档规范复审与真实数据验证（2026-08-04）](./report/memory-archive-compliance-audit-2026-08-04.md) — 383,718 条真实记录只读扫描、主体归属边界与全项目回归证据
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
- [商业级具身架构](./architecture/Minecraft具身架构.md) — Agent、完全仿生、OBS 与服务器路线
- [Minecraft 生产运行手册](./operations/minecraft_production_runbook.md) — 固定环境、部署、就绪、烟雾测试与故障恢复
- [2026-08-04 商业级审计与生产验收报告](./report/minecraft-commercial-audit-2026-08-02.md)
- [2026-08-04 生产具身集成验收](./report/minecraft-integration-acceptance-2026-08-04.md) — 二号独立审查、0.2.1 重放/首次启动补强与最终现场门

## 她的日记

- [diaries/](./diaries/) — 2026-05-03 的六篇日记

## 历史归档

- [analysis/](./analysis/) — 架构分析、主动性分析、情绪诊断、梦境机制等
- [plans/](./plans/) — 各阶段技术方案与计划（2026-03 至 2026-06）
- [archive/](./archive/) — 开发报告、审计记录（不代表当前状态）
