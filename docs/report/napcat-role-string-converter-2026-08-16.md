# NapCat 入站 role 字符串转换失败（2026-08-16）

## 证据

2026-08-15 23:58:55 与 23:59:00，`napcat_adapter` 收到两条消息后在统一转换器失败：`AttributeError: 'str' object has no attribute 'value'`，位置为 `src/core/transport/message_receive/converter.py` 的 `sender_role` 归一化。

## 根因

`UserInfoPayload` 的进程内类型声明使用 `UserRole` 枚举，但 NapCat 的 JSON 入站数据携带枚举的 wire string（例如 `member`）。转换器无条件访问 `.value`，导致消息在进入统一消息总线前被丢弃。

## 修复

转换器现在兼容 `UserRole`、非空字符串和带字符串 `.value` 的兼容对象；空字符串归一化为无角色，未知对象保留其字符串形式。没有改变消息正文、发送目标、历史事件或权限语义。

## 验证

- 新增字符串/枚举两种输入的回归测试。
- 消息转换器与多媒体转换器定向测试：14 passed（使用 `--no-cov`；单测覆盖率门槛不适用于该小套件）。
- 语法检查通过。
- Ruff 对本次新增测试无报错；转换器仍有既有的 10 条基线告警，未在本次扩大范围处理。

## 运行与回滚

本次未启动、停止或重启 Elysium/NapCat，也未修改任何正式数据。回滚只需撤销 `converter.py` 的 role 归一化 hunk并移除对应测试；权威消息历史不受影响。
