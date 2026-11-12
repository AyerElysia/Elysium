# 2026-11-09 Spark 未提交代码整理

本次按用户请求在原仓库审查、修复并分组作本地提交。
父仓库起点 aff462d9，Elysia-Harness 起点 0b0a237698。
开始时工程快照与校验清单位于本机私有 /tmp/elysium-commit-review-20261109-6az6yno_/；
只备份工程差异与未跟踪工程文件，没有沿 Data 符号链接读取主体资料。

## 提交分组

- 父仓库 d616a007：保留 10 月 23 日旧 Harness 审计，明确原日期与版本。
- 父仓库 95ce7e53：Gemini 中转、模型超时和 QQ 登录恢复的工程记录；
  原诊断的阶段结论已按后续证据直接修正，不把过去状态冒充当前验收。
- 父仓库 306c0599：NapCat 默认关闭与精确 owner/故障证据/有限恢复保护。
- 父仓库 90a7269f：grep 对外 schema 使用 auto，兼容旧空字符串调用。
- 父仓库 6e9ad34c：派生请求协议闭合、精确事件副本及失败回滚，不改写权威历史。
- 子仓库 ee1ab50387：agent-scope 文件能力及搜索、编辑、卸载边界；同步 peer/importer 与文档。
- 子仓库 d29174856f：主体修订工程流程，明确开发代理不能代主体改写已实例化人格/记忆。
- 本记录与父仓库的两个 gitlink 同次提交：Elysia-Harness 指向 d29174856f，
  Ayla 记录现有干净 checkout e202f446；没有在 Ayla 内修改代码或切换版本。

## 验证与限制

- Conscious-mind 最终正常 Vitest 配置 10 文件、57 用例通过；
  五插件 TypeScript 无输出检查零诊断。
- 离线 frozen-lockfile 的 conscious-mind 开发依赖链接安装成功，
  锁文件策略门禁通过；另核对 profile YAML、新 exports、peer 元数据与 importer。
- Life Engine 指定四文件 186 passed / 3 deselected，另两份纯域上下文测试 22 passed。
  三项 recovery-policy 用例已在进程内载入 HEAD 源码单独复核，仍为 3 failed；
  本次未修改那块维护授权策略，不把排除后的结果写成完整四文件全通过。
  对应现存缺陷、测试节点、源码 SHA 与日志见 life-engine-dirty-review.md。
- NapCat 两文件隔离回归 57 项通过，F/E9 与新增测试 Ruff 通过。
  Life Engine F/E9 与 HEAD 相同，无新增；存量 10 F401 + 2 F841 未扩大清理。
- 所有测试仅用合成事件、mock 和自有夹具，不竞争正式端口或存储 authority。
  主体文件、会话历史、生产数据库、模型调用和平台消息均未用于测试。
- 每次提交前均核对精确暂存文件名单与 git diff --cached --check；
  暂存内容按 hash 对照，混合文件使用单独 index 内容，不覆盖工作区的本机改动。
- 子仓库提交钩子提示找不到 lefthook/配置；没有跳过钩子，
  上述相关测试、类型检查和暂存检查由本次实际运行提供证据。

## 刻意保留的工作区状态

Elysia-Harness 本机 Web UI notice/临时公网隧道配置、两个启动脚本本机差异，
以及 lock 中两处无关 Node 类型 22→26 漂移未提交。
28 个 agent-loop/src 编译生成文件、elyharn_guard.sh 保留不提交。
固定 cwd 的本机 Data alias 与提示词备份目录加入 ignore，不把主体内容或部署路径链接写入历史。

父仓库两份 NapCat .bak 与 elysium_guard.sh 保留不提交。
未安装或提交 Elysium/ElyHarn 后台自动拉起脚本；
NapCat 新恢复开关保持默认关闭，没有修改真实配置或执行 socket 清理。
保留文件的内容与开始快照 hash 一致。

## 整理期间出现的并行改动

最终核对时，Elysia-Harness 的 plugins/kook-bot/src/gateway.ts 与 tests/kook.spec.ts
新增了心跳、Pong 和重连改动；它们不在本次开始快照内，也不是本任务子代理的修改。
为保护仍在进行的并行工作，这两份工程改动保留在工作区，未暂存、提交、覆盖或还原。
上述本次测试与提交结果不构成这两份新改动的审查或验收。

## 发布边界与回退

本次没有 push、合并远端、更新线上 file: 安装副本、启动或重启服务。
运行副本必须在单独部署任务中核对哈希、真实启动、长期 session 与关键链路；
验收前仅保留本地提交，按父 AGENTS.md 不推送。
后续推送须先发布子仓库提交，再核对父 gitlink；fetch 后检查并发分叉，不 force 覆盖。

未迁移生产数据，也没有代写主体正文。源码可按独立功能提交撤销，
不通过删除/重建 session、清空历史或整库覆盖进行回退。
