# life_engine 13/14 启动失败：proactive 首次绑定未完成

- 时间：2026-08-24
- 现象：Elysium 加载 13/14 插件，失败插件为 `life_engine`
- 当时错误：`ProactiveMigrationCertificateMissingOrAmbiguous`

## 纠正后的结论

先前把现场理解成“selectable 库已有 v2 binding 链，只需同后端 generation repair”。这是错的。JSON cache 不是权威；权威锚在 `runtime_events` / `runtime_states` 的 binding 链上。

实际状态：

- `config/core.toml` 指向 verified generation `local-selectable-20260824-v3`
- `data/life_storage/local.sqlite3` 有主动历史和一条 v3 copy 证书，但 **binding 链为空、head 为空**
- copy 证书的 `source_binding_sha256` 等于 leftover `data/life_engine_workspace/runtime/proactive/proactive.sqlite3` 的 v1 链尖 `d3ee152c…`，不是 JSON cache 的 v2 `3430e2ec…`
- 证书的 `target_backend_identity_sha256` 是引导候选路径
  `sqlite:////root/Elysia/backups/local-selectable-v3-candidate-data-20260824/life_storage/local.sqlite3`
  → `6ba1c4dd…`；当前生产路径
  `sqlite:////root/Elysia/Elysium/data/life_storage/local.sqlite3`
  → `72a79bba…`
- JSON cache 停在 v2，且已经使用当前生产路径 identity；它不能被提升成 chain

因此启动 `ensure_proactive_backend_binding` 既不能消费 copy 证书（路径 identity 与源 binding 都不匹配），也不能走 generation repair（没有 chain）。含糊的 `CertificateMissingOrAmbiguous` 是“唯一证书证明了历史，但不证明当前 sqlite 路径”被折成了零匹配。

这不是主体语义问题，也不应再跑一次 snapshot copy。正确控制面是显式完成 **relocated verified copy 之后的首次生产绑定**。

## 证据

- `data/life_storage/local.sqlite3`：`life_proactive.backend_binding` 事件 0 行、head 0 行；`life_proactive.migrations` 1 行，`migration_id=proactive-local-selectable-20260824-v3`
- leftover workspace sqlite 仍有 v1 binding `life-proactive-local-v1` / `d3ee152c…`，与证书 source 一致
- `backend-binding.json`：`local-selectable-20260823-v2` / `3430e2ec…`，identity 已是 `72a79bba…`
- `/root/Elysia/backups/local-selectable-bootstrap-20260824-v3/bootstrap_report.json` 记录候选库 identity `6ba1c4dd…`；live 与 candidate sqlite 同大小、不同 inode

## 代码与运维修正

1. 同后端 generation bump（**已有 chain**）仍走 `repair_proactive_generation_binding`；空链时改为 `ProactiveBackendBindingChainMissing`，不再报 `AnchorsDiverged`。
2. 空链 + 已验证 copy 证书证明 live history、但证书路径 identity 漂移时，启动失败关闭为 `ProactiveMigrationCertificateBackendIdentityMismatch`。
3. 运维命令 `complete_proactive_initial_binding` / `scripts/repair_proactive_generation_binding.py --complete-initial-binding --apply`：用 leftover 源 sqlite 的 certified binding 作为 source，显式确认证书上的旧路径 identity，在当前生产路径写下 epoch-1 的 v3 锚，并覆盖 JSON cache。禁止把 v2 JSON 提升成 chain。
4. 同后端 generation repair 若只缺 head，可从 chain 尖恢复 head。

## 验收

- generation repair：改绑前 `ensure_*` 报 `ProactiveGenerationRepairRequired`；缺 head 可恢复；空链必须 `ChainMissing`。
- relocated copy：不确认证书 identity 时失败；用 stale cache 当 source 时失败；确认后 `verify_*` / `ensure_*` 健康。
- 对本机先诊断：`chain_length=0`、`empty_chain_initial_bind_required=true`、证书 identity 为 `6ba1c4dd…`、live identity 为 `72a79bba…`、roots 匹配。然后带 `--complete-initial-binding --certificate-backend-identity-sha256 6ba1c4dd…` 应用。
- 2026-08-24 已对本机 `data/life_storage/local.sqlite3` 执行 `--complete-initial-binding --apply`。事后诊断：`bound_generation_id=local-selectable-20260824-v3`，`chain_length=1`，`head_present=true`，`marker_equals_head=true`，`verify_status=healthy`。copy 证书仍记录候选路径 identity，这是预期，不需要再改证书。
- 不启动、不停止用户的 Elysium 进程。用户手动重启后应看到 14/14，且不应再出现证书歧义错误。
