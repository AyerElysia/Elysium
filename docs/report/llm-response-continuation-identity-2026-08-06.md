# LLMResponse 续轮轨迹身份修复报告

日期：2026-08-06

## 目标

修复 `LLMResponse.send()` 创建工具续轮时丢失主体轨迹关联身份的问题，使 reasoning、工具参数、工具结果和续轮回复能够在训练数据与审计记录中归属于同一主体轨迹。

## 实现

- `LLMRequest` 新增仅限关键字传入的 `trace_id`、`stream_id`、`heartbeat_run_id`、`call_id` 关联字段，不改变既有位置参数兼容性。
- `LLMResponse.send()` 在真正发送续轮时，从当前 `_upper` 读取上述关联字段，并浅拷贝 `trajectory_metadata`。
- 每轮 `request_id` 和每次 provider 尝试的 `attempt_id` 仍由内核独立生成，不继承、不复用。
- 只继承 content-free 身份和元数据；payload、reasoning、工具正文不会被注入元数据。
- 顶层元数据字典不共享，续轮修改不会回写上一轮；发送前对 `_upper.trajectory_metadata` 的更新会进入续轮快照。

## 验证

- 续轮与请求定向：`86 passed`。
- LLM 内核全集：`582 passed`，仅有 2 条既有第三方 websockets 弃用告警。
- 全仓：`3738 passed / 17 skipped / 3 warnings`，覆盖率 `68.26%`。
- 变更文件 Ruff `F/E9/I`、`compileall`、`git diff --check` 全部通过。
- 合同测试覆盖关联 ID 继承、元数据发送前更新、顶层字典隔离、正文不进入元数据，以及独立 request/attempt ID。

## 运行边界

本次没有启动、停止或重启 Elysium、NapCat/QQNT 或其他服务，也没有修改生产配置与正式数据。代码会在用户下次手动启动 Elysium 后生效。
