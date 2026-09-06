# life_engine 配置无法加载（2026-09-05）

> 时间：2026-09-05 15:50 第一次失败；16:30 / 16:32 第二次；16:44 / 16:48 第三次（13/14 插件）
> 现象：`life_engine` 在启动事务中失败，心跳未启动。Elysium 主进程仍可能带着其余插件继续跑。

## 证据

### 15:50 第一次

```text
插件 'life_engine' 加载失败: 插件启动事务失败: Cannot declare ('learning',) twice
```

`tomllib` 拒绝同一 TOML 表声明两次。当时 `config/plugins/life_engine/config.toml` 末尾有两段顶层 `[learning]`：前一段是三环学习节，后一段其实是机会运行时（文案为「统一机会运行时」），节名误写成 `learning`。

### 16:30 / 16:32 第二次（纠正）

上一版报告写「学习节空字符串会在 auto_update 时按类型回落到默认值」。**这句话是错的。** 重启后 TOML 已能解析，失败变成：

```text
2026-09-05T16:30:51.208 | ERROR | plugin_manager | 插件 'life_engine' 加载失败: 插件启动事务失败: 1 validation error for LifeEngineConfig
learning.model_task_name
  String should have at least 1 character
```

16:32:19 再次出现同一错误。没有 Python traceback；`plugin_manager` 只记录 `str(exc)`。

当时 `[opportunity]` 已正确存在，但 `[learning]` 仍是：

```toml
model_task_name = ""
```

`LearningSection.model_task_name` 有 `min_length=1`。合并逻辑只用 `TypeAdapter(str)`，空字符串类型合法，被写回 merged 配置，随后整模 `model_validate` 失败。`auto_update` 在校验失败前就会按 merged 回写文件，所以空字符串不会自愈。

### 16:44 / 16:48 第三次

配置已能加载，记忆恢复也完成，随后启动事务失败：

```text
2026-09-05T16:48:20.919 | INFO  | life_engine | life_engine 已停止
2026-09-05T16:48:20.920 | ERROR | plugin_manager | 插件 'life_engine' 加载失败: 插件启动事务失败: OpportunitySchemaNotReady:missing_table:opportunity_provider_events
```

16:44:35 已是同一错误。控制台「life_engine 已停止」是启动事务回滚后的插件清理，不是完整 Elysium 进程退出。当时主进程仍在，`data/runtime/elysium.lock` 指向 `main.py`。

只读核验正式库 `data/life_storage/local.sqlite3`：没有任何 `opportunity_*` 表，也没有 `opportunity_runtime_meta` 切换标记。`prepare_opportunity_runtime.py` 从未对正式数据执行 `--apply`。

`LifeEngineService.opportunity_managed` 在 durable marker **或** `opportunity.enabled=true` 时为真。业务启动调用 `open_opportunity_stores(..., initialize_schema=False)`，缺表必须失败关闭，禁止在启动时建表。第二次修复把误写的机会节改名为 `[opportunity]` 时，把复制来的 `enabled = true` 留了下来；架构默认与运维手册都要求该开关保持 false，直到独立 schema 准备完成。

## 根因

1. 机会节曾被渲染成与学习节同名的 `[learning]`，得到非法 TOML。
2. 合并只校验 Python 类型，不校验 Field 约束。TOML 占位 `""` 对 `str` 通过，对 `min_length=1` 在最终校验失败。
3. 未完成机会 schema 准备就把生产开关设为 `opportunity.enabled = true`。代码按设计 fail closed；这不是缺表被忽略，而是把尚未准备的运行时当成已切换。

上一版报告写「本次只解除配置加载阻断」，并保留 `enabled = true`。**那会让第三次失败必然发生。** 生产开关在独立准备完成前必须是 false。

## 已执行操作

- 第二段改为 `[opportunity]`，`poll_interval_seconds = 5.0`。
- `_iter_sections()` 在两个字段映射到同一 TOML 节名时显式失败。
- 将运行配置 `learning.model_task_name` 恢复为 `"learning"`。
- `_merge_section_fields` 按整节 Field 约束校验单字段；`""` 这类非法值回落到模型默认值后再回写。
- 将运行配置 `opportunity.enabled` 恢复为 `false`（架构默认；不是正式 cutover）。
- `_initialize_opportunity_runtime` 在 `OpportunitySchemaNotReady` 时仍失败关闭，并指出须先走 `scripts/prepare_opportunity_runtime.py`。
- 定向测试覆盖：重复节名 fail closed；`learning`/`opportunity` 节名唯一；空 `model_task_name` 经 auto_update 恢复为 `"learning"`；默认 `opportunity.enabled` 为 false；未 managed 时不 attach；`enabled=true` 且缺表时仍 `OpportunitySchemaNotReady`。

未执行：未对正式库 `--apply` / `--mark-managed`，未改主体文件，未停止或重启正在运行的 Elysium 主进程。

可逆性：运行配置开关改回默认 false；配置合并器与错误包装可随代码回退。正式库未改。

## 验证

```bash
uv run --group dev python -m pytest \
  test/kernel/test_config.py::TestConfigBase::test_duplicate_toml_section_names_fail_closed \
  test/kernel/test_config.py::TestConfigBase::test_auto_update_recovers_empty_constrained_string \
  test/plugins/life_engine/test_config_validation.py::test_life_engine_toml_section_names_are_unique \
  test/plugins/life_engine/test_config_validation.py::test_opportunity_runtime_defaults_disabled \
  test/plugins/life_engine/test_opportunity_service_integration.py::test_unmanaged_startup_does_not_attach_opportunity_runtime \
  test/plugins/life_engine/test_opportunity_service_integration.py::test_enabled_switch_without_schema_fails_closed_and_names_prepare_script \
  test/plugins/life_engine/test_opportunity_storage_contract.py \
  test/plugins/life_engine/test_prepare_opportunity_runtime.py \
  -q --no-cov -n 0
```

启动链路须用户手动停止当前仍在运行的 Elysium 主进程后再手动启动。Agent 不得代为启停。重启后不应再出现 `Cannot declare ('learning',) twice`、`learning.model_task_name` 空字符串校验失败，或在 `opportunity.enabled=false` 且无 cutover marker 时出现 `OpportunitySchemaNotReady`。

## 待决

1. 机会运行时是否进入 managed 模式，仍取决于正式维护窗口内的 schema 准备、`--mark-managed` 与主体安装能力；三者不能互相冒充。见 [机会运行时切换与验收](../operations/opportunity-runtime-cutover.md)。
2. 用户需先确认当前 `main.py` 进程已退出，再手动启动一次完整 Elysium，以启动成功及 `life_engine` 心跳日志作为验收证据。
