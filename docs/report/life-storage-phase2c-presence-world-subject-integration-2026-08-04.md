# 生命域可选存储阶段 2C：Presence/World 迁移与 Subject 现役接线

日期：2026-08-04

## 结果

本阶段完成两件此前仍缺失的工作：把 Presence/World 从真实只读快照无损复制到
远程 MySQL 并反向恢复；把 Subject Document 接入 `LifeEngineService` 唯一拥有的
`StorageBackendRuntime`，让主体文件的真实写入路径执行“先入不可变账本，再投影
工作区”。没有启用正式 selectable storage，没有停止或重启 Elysium/NapCat，
也没有移动、删除或改写任何源 SQLite、JSON、Markdown、JSONL 或媒体文件。

## Presence / World 实现

- 新增显式快照选择、规范化、逐表 hash、聚合根、候选复制、反向 SQLite 导出和
  独立只读审计；迁移不接受任意表或万能 JSON 归档。
- MySQL schema 初始化允许持有有效 fencing token 的 `CANDIDATE_COPY` writer，
  并在迁移前后复验 writer；disabled、失效 token 和非受控 writer 继续拒绝。
- Presence 保留 revision、lease、stream owner 与 lifecycle outbox；World 保留
  source-preserving assertion/change、opaque frontier、perception cursor 和重建状态。
- 仅允许修复“全新 World 目标由 schema 初始化产生 frontier=0”这一种已证明无业务
  数据的候选状态；目标只要已有 assertion/change/cursor，任何不同证据均显式冲突。

## 真实数据证据

源快照：
`C:\Temp\Data\ElysiumBackups\life-domain-20260804T0615Z-candidate`

- 快照 manifest SHA-256：
  `77435387f4acc59e48ffe625015d07575398465534b132e428c47e4617124862`。
- 快照 source root：
  `d8f800108c71203396f9e6c39e8aa0a386ce7521d2d60ea333ee4ca13ff5a724`。
- 成功批次：`life-presence-world-shadow-v3-77435387f4acc59e`，状态
  `copied`，冲突 0。
- 源业务记录共 2,031：Presence 35、stream owner 0、outbox 895；World
  projector meta 3、assertion 108、change 983、perception cursor 7。
- MySQL 另有 3 条由当前 World 合同生成的 policy/schema/rebuild 元数据；它们与源
  记录分开审计，没有冒充源数据。
- World frontier 为 86094，policy 为 `source-preserving-v1`，rebuild 为 idle。
- 源、MySQL 业务投影与反向 SQLite 聚合根一致：
  `abd4f799f8208bc9edafdc3fb67a486486ba7746d599afd4fd5b0a341e5a2214`。
- 反向恢复目录：
  `C:\Temp\Data\ElysiumBackups\life-presence-world-reverse-20260804T1030Z`。
- 反向 manifest SHA-256：
  `86a0b6f9119d9462ff3c51506ecb1407f566ba31a34bcc09b2a05a94b9107391`。
- 独立审计：`verified=true`、缺失/多余/不一致表均为 0、incomplete marker 不存在。

两个较早批次完整保留失败证据：v1 被旧 candidate schema 权限门拒绝，0 业务行；
v2 发现 schema 初始化的 virgin World frontier 与源 frontier 不同并 fail closed，
0 源业务行。v3 只在确认目标无 assertion/change/cursor 后执行上述受限修复。

## Subject 现役写链路

- Subject store、observer、projector 与 Life Event/Memory/Presence/World 使用同一个
  service-owned runtime；关闭前先解绑消费者，runtime 只关闭一次。
- `nucleus_write_file`、`nucleus_edit_file` 对 SOUL/USER/MEMORY/diaries 的写入
  先执行 exact-byte version/head CAS，再由 parent-hash projector 原子替换工作区。
- 外部文件变化先作为“语义来源未知”的精确观察版本追加；未知字节不会被覆盖。
- Memory Witness 在 selected 模式下以 `witness_id` 为 occurrence、对应意识实例为
  actor、Memory witness 为 source 完成写前入账；disabled/local 保持原子文件写入。
- 重复写相同字节是 no-op，不增加版本；Subject 健康状态进入 service 聚合健康。
- 通用 `nucleus_bash` 在 selected 模式下 fail closed，防止通过脚本、变量或间接
  命令绕过写前入账；专用 file/领域工具仍可完整操作主体文档。

## 验证与安全结论

- Presence/World 快照迁移、反向导出和审计专项通过。
- Subject local exact-byte 写入、targeted projection、重复写、健康与关闭合同通过。
- Witness selected/local 双路径和通用 shell fail-closed 回归通过。
- 阶段专项：47 passed；完整 Life Engine：830 passed / 6 skipped，全部功能
  用例通过（子集覆盖率 39.22% 低于全仓 40% 门，仅该 coverage 门非零）。
- 全仓：3,447 passed / 12 skipped，coverage 66.38%，退出码 0。
- 变更文件 Ruff、compileall 与 `git diff --check` 通过；共享大文件只执行
  F/E9 回归，未顺带格式化既有代码。
- 真实远程复制没有写入凭据到仓库或文档；连接信息只由进程环境提供。
- `writer_frozen=false`、`generation_eligible=false`、`storage.enabled=false`；本批
  数据不是可激活 generation。正式切换仍要求用户批准冻结窗口、数据库级不可变
  保护、隔离 MySQL 合同、全领域追平与真实运行闭环。

定向 Life Engine 子集的 coverage-only 门槛与功能测试结果已分开记录，未把覆盖率
门槛误报为业务失败；最终全仓覆盖率已通过项目门。
