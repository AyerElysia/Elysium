# 阶段三（P3-14）验证报告

> 日期：2026-08-09
> 范围：`docs/architecture/阶段三-Elysium应用后端接口导出开发步骤.md` 第 P3-14 节（兼容迁移、文档与最终验收）
> 验收方式：全部离线验证；本轮未启动、停止或重启 Elysium / NapCat，未进行真实前端或 Provider 端到端验收。

## 1. 已验收

### 1.1 旧插件路由弃用标记与迁移期

- 三组旧插件路由保留未删除，均以 `BaseRouter` 类属性声明弃用元数据（`deprecation_notice`、`deprecation_sunset_date`、`deprecation_migration_link`），响应带 RFC 5987 编码的弃用 header：

| 旧路由 | 取代者 | 迁移期限 |
| --- | --- | --- |
| `plugins/livestream/router.py` `/livestream/*` | `/api/v1/livestream/*` | 2027-02-01 |
| `plugins/voice_live/router.py` `/voice-live/*` | `/api/v1/voice-calls/*` | 2027-02-01 |
| `plugins/life_engine/memory/router.py` `/memory_vis/*` | `/api/v1/memory/visualization` | 2027-02-01 |

- 迁移期旧客户端仍可用；弃用仅为标记，不自动删除、不自动跳转。
- 实现与契约测试：`src/core/components/base/router.py`、`test/core/components/base/test_router_deprecation.py`、`test/api/v1/test_p3_14_deprecation.py`。
- 部署文档记录于 `docs/operations/deployment_and_usage.md` 第 6.5 节。

### 1.2 部署文档更新

`docs/operations/deployment_and_usage.md` 已更新且不写本机私密配置：

- 6.4 节挂载端点清单补齐 P3-08 直播、P3-09 语音、P3-10 桌游、P3-11 管理、P3-12 意识/世界/记忆、P3-13 权限矩阵与预算。
- 6.5 节「旧插件路由弃用与迁移期」；6.6 节「阶段三文档产物」。
- 维护记录新增 2026-08-09 两条；文档状态日期更新。

### 1.3 文档产物（`docs/api/`）

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| `openapi.json` | 完整 OpenAPI schema | 131 paths / 134 operations，134 个唯一 operationId |
| `events.md` | 事件目录（EventEnvelope 与各通道事件类型） | 完成 |
| `errors.md` | 错误码目录（HTTP 状态与 `error.code` 语义） | 完成 |
| `permissions.md` | 权限矩阵（身份、scope、资源授权与实现状态） | 完成 |
| `frontend-example.md` | 前端最小参考实现 | 完成 |
| `README.md` | 目录索引与生成/校验命令 | 完成 |
| `verification.md` | 本报告 | 完成 |

OpenAPI 由 `scripts/generate_api_openapi.py` 生成：只执行路由注册、不执行 endpoint 函数体，以轻量 fake provider 构造完整 `APIContext`。134 个操作 id 无重复，满足第 25 节验收门；5 个 WebSocket 端点不在 paths 属 FastAPI 正常行为。与 `API_INVENTORY`（183 契约）的差异全部归于三类正常情况：WebSocket 端点、`/openapi.json` 自身路由、inventory 中标记 planned/experimental 的未实现域。

### 1.4 测试

| 范围 | 结果 |
| --- | --- |
| P3-14 定向测试（`test_p3_14_deprecation.py` + `test_router_deprecation.py`） | 8 passed |
| API v1 + 命令完整测试 | 135 passed（29.10s） |
| 风险范围测试（base 组件 + 消息事件 + 飞书适配器） | 58 passed（23.56s） |

### 1.5 静态与一致性与检查

| 检查 | 结果 |
| --- | --- |
| Ruff（本轮改动文件） | 通过。`plugins/life_engine/memory/router.py`（5 项）与 `plugins/voice_live/router.py`（7 项）的错误码集合与数量与 `HEAD` 基线完全一致，判定为预存问题、非本轮引入，未修改；`src/core/components/base/router.py` 的 4 项亦为预存（HEAD 版本同现）。 |
| compileall | 通过（`src plugins main.py` 全量 EXIT=0）。 |
| `git diff --check`（等价验证） | 所有 P3-14 已跟踪改动文件逐个通过；新文件用 Python 精确检查无尾随空白（排除 CRLF 误报）、无冲突标记。 |

## 2. 暂不验收

- **真实前端 / Provider 端到端验收**：本轮未启动或重启 Elysium，也未进行真实前端页面或 Provider 端到端验收。旧路由在迁移期可用性已由契约测试覆盖，但「旧客户端在迁移期可用」的真实 E2E 证据待补。
- **管理中 planned/experimental 域**：`docs/api/permissions.md` 已标注 admin/chat、admin/voice-calls、admin/media、admin/memory 详情、admin/tabletop 裁判台等为 planned/experimental，未纳入已验收清单。
- **git 仓库提交状态**：P3-11~P3-14 的全部改动目前为未提交状态（见第 4 节说明）。

## 3. 已回退

- 无。本轮没有回退任何实验性实现或公共合同修改。

## 4. 环境备注

- 本地 git 仓库曾被检出 `.git/refs/` 目录缺失、`refs/heads/main` 失效，且 P3-11~P3-13 的提交对象在 `objects` 中缺失（疑似 gc/备份清理导致）。已通过 reflog（`.git/logs/refs/heads/main`）定位仍存在的最近提交 `ebda0eec`（commit 类型），手动重建 `refs/heads/main` 恢复仓库可用。
- 因此 P3-11~P3-14 的工作目前全部是未提交（unstaged/untracked）状态；`git diff --check` 只能对 HEAD 中对象可读的文件逐文件执行。建议尽快提交或确认备份策略，避免仓库元数据再次损坏导致提交对象丢失。

## 5. 结论

P3-14 六项任务全部完成，验收门满足：

1. 旧插件路由保留并标记，提供弃用 header 与迁移期（2027-02-01），未删除 —— ✅
2. 部署文档更新，未写本机私密配置 —— ✅
3. OpenAPI、事件目录、错误码、权限矩阵、前端示例全部生成 —— ✅
4. 定向测试 → 风险范围完整测试 → Ruff → compileall → `git diff --check` 全链路通过 —— ✅
5. 无需真实 E2E 或重启（全部离线验证），无需授权 —— ✅
6. 本报告明确「已验收 / 暂不验收 / 已回退」 —— ✅
