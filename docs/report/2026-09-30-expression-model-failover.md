# 2026-09-30 表达模型冷却与故障转移

## 现象

表达层日志出现 LLMEmptyResponseError，随后所有候选模型进入冷却，life_chatter 无法生成表达内容。

## 变更

将 config/models.toml 的 tasks.expression 模型顺序设为：

gpt-6.1-sol -> gpt-6-sol -> gpt-6-luna

DasuAPI-Claude 仍是当前优先渠道。D1 api 继续不作为本次链路依赖，因为其现有备注记录了经中转后出现空角色和空内容的问题。

## 校验

gpt-6.1-sol、gpt-6-sol、gpt-6-luna 均完成最小请求探针并返回 HTTP 200。仅改配置，未自动重启 Elysium；需要人工重启后新链路才会被运行进程加载。
