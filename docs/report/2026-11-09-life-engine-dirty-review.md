# 2026-11-09 Life Engine 未提交改动审查

本次在 Spark 父仓库检查已有 Life Engine 工程改动，原始完整工作区文件及差异保存于 `/tmp/elysium-life-engine-review-h8mnlfbn`。本文是本次隔离验证记录，不构成之后版本或正式运行链路的持续担保。

## 功能分组与补正

1. **派生请求协议闭合**：`core/chatter.py`、`core/trajectory.py` 与对应两个测试。合并派生视图中的相邻 assistant；补正新闭合 helper 无条件丢弃前帧调用的问题，只移除无保留回执的前帧调用。回归测试先复现调用丢失，再验证保留 live call/result、协议有效及输入列表不变。轨迹压缩测试增加完整协议校验和导出列表隔离校验。
2. **心跳事件精确副本**：`service/core.py`、`service/subconscious_context.py` 与 `test_heartbeat_visible_projection.py`。原改动按第一份副本保留，遗漏后来的已消费标记；历史追加还会直接丢弃带确认的新副本，心跳输出的 pending 副本也可能失去确认。新增测试先复现这三种情况，再统一为完整事实一致才合并、已确认副本优先、与顺序无关。收尾时另复现失败回滚按 ID 清掉既有确认、覆盖不同事实两项问题；现改为只恢复本轮实际改过的对象，并按完整事实合并恢复历史。不同事实复用同一 ID 仍保留并由 visible prepare 拒绝。
3. **grep 参数 schema**：`tools/grep_tools.py` 与 `test_tool_manifests.py`，将空默认枚举改为显式 `auto`，执行层兼容旧空字符串。该组已有测试通过，未额外修改其工程代码。

稳定行为另见 [Life Engine 上下文投影](../architecture/life-engine-context-projection.md)；grep 参数兼容另见 [排序 schema](../architecture/grep-sort-schema.md)。

## 定向验证

所有事件为合成数据，服务由 `__new__` 构造并 mock 外部边界，轨迹文件仅写 pytest 临时目录。使用现成开发依赖、`UV_OFFLINE=1`、`uv run --no-sync --group dev`、单 pytest worker、关闭 coverage 和 cacheprovider；未启动服务、调用模型/平台或访问正式存储。

- 四个指定文件运行结果：**186 passed，3 deselected**。排除项仅为下述已经单独确认的 HEAD 既有失败。日志：`/tmp/elysium-life-engine-review-h8mnlfbn/pytest-final-directed.log`。
- `test_subconscious_context.py` 与 `test_subconscious_interleaved_frontier.py`：**22 passed**。日志：同目录 `pytest-domain.log`。
- 精确副本及回滚的新增回归先复现后修复，日志分别为同目录的 `duplicate-reproduction.log`、`rollback-reproduction.log`；live 调用丢失复现见 `live-receipt-reproduction.log`。
- 指定九个变更文件的 Ruff `F,E9` 与 HEAD 逐诊断比对：**无新增**；HEAD 和当前各有 10 个 F401、2 个 F841 存量诊断，未扩大清理。
- `git diff --check` 通过。

九个 service 测试的初始失败源于旧 fixture 的 120 字符预算及无来源文本期望。当前服务使用含时间、来源、ledger 标识的文本；fixture 调整为 512 字符，并显式断言实际完整可见因果组不超过预算、原始正文大于预算，保留整组预算和精确投递测试的含义。

## 已确认的 HEAD 既有失败

`test_chatter_prompt.py::test_context_recovery_turn_blocks_visible_action_side_effect[0]`、`[4]`、`[99]` 在本次最初测试均失败。为区分新增改动，在独立进程内直接加载 `git show HEAD:plugins/life_engine/core/chatter.py` 的源码后只运行这三个节点，结果仍为 **3 failed**，均触发 mock 的 `maintenance turn must not execute visible actions` 断言。该过程未替换工作区文件。

复核源码 SHA256：`35607f20c3824a2ca8c302e25243c1e34f3ddbfbd21f06082949e28b867eced5`。日志：`/tmp/elysium-life-engine-review-h8mnlfbn/head-recovery.log`。

在该基线源码中，TOOL_EXEC 计算了 `compression_turn_required`，但普通调用循环执行前未调用已有的 `_is_context_stewardship_call`；维护续轮分支位于工具执行之后。这块不属于原 dirty 差异，本次按提交整理范围不扩展修改其维护授权策略；该既有缺陷保留为单独后续事项。不能将排除这三项后的通过结果描述为完整四文件全部通过。

本次未暂存、提交、推送或重启，也未改写主体、记忆、私有历史或生产数据库。
