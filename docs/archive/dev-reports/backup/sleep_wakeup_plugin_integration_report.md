# sleep_wakeup_plugin 集成报告

## 结果

已将 `/root/Elysia/Neo-MoFox_Deployment/Neo-MoFox/plugins/sleep_wakeup_plugin.7z` 集成为仓库内可加载插件：

- 插件目录：`plugins/sleep_wakeup_plugin/`
- 默认配置：`config/plugins/sleep_wakeup_plugin/config.toml`
- 保留核心能力：
  - 定时推进困倦值状态机
  - sleeping 状态下消息拦截
  - 私聊消息触发唤醒调整
  - guardian LLM 决策
  - 睡眠报告注入 `actor` reminder
  - JSON 持久化运行状态

## 适配内容

- 按仓库规范改成标准插件结构，移除压缩包内 `__pycache__`
- 统一为仓库当前可用的导入入口：
  - `src.app.plugin_system.base`
  - `src.app.plugin_system.api.llm_api`
  - `src.app.plugin_system.api.storage_api`
- 插件类只在 `configs` 中声明配置，不再把配置类塞进 `get_components()`
- 统一改为包内相对导入，避免运行时模块路径不一致
- 增加仓库默认配置文件，保证插件能直接加载

## 持久化说明

- 运行态通过插件存储 API 持久化到：
  - `data/json_storage/sleep_wakeup_plugin/runtime_state.json`
- 因此重启后状态可恢复
- 但插件本身包含“跨天/非睡眠期自动清理旧状态”的逻辑，这属于插件设计行为，不是持久化失效

## 风险与说明

- `guardian_model_task` 默认使用 `actor`，因此不依赖额外新增模型任务
- 当前仓库的 `config/model.toml` 里未看到 `[model_tasks.diary]`，这是与本插件无关的独立问题
- 本次集成主要验证了静态导入、语法、linter 和目标测试路径，不包含真实 IM 平台在线联调
