# 工具调用高性能化与 Claude Code 风格改造方案

日期：2026-04-03

## 目标

把 Neo-MoFox 当前的工具调用链改造成更接近 Claude Code 的工作方式：

1. 支持同一轮内的安全并行工具调用，而不是现在的逐个串行执行。
2. 把文件搜索从 Python 递归扫描升级为 `ripgrep`/`fd` 风格的高性能路径。
3. 让“搜索、定位、读取、编辑、委派子任务”这几类能力职责更清晰，降低模型规划成本。
4. 保持现有上下文协议不破，不引入“并行后上下文非法”这种回归。

## 现状结论

### 1. 模型层已经基本具备并行能力，但编排层没接上

- `src/kernel/llm/model_client/openai_client.py:882` 已经允许透传 `parallel_tool_calls`。
- `src/kernel/llm/request_inspector.py:43` 也已经能展示 `parallel_tool_calls`。
- 但真正执行 tool call 的地方仍然是串行：
  - `src/core/components/base/chatter.py:522` 的 `run_tool_call()` 只负责单个调用。
  - `plugins/default_chatter/tool_flow.py:48` 按 `for call in calls` 串行执行。
  - `plugins/life_engine/service.py:1529` 心跳内逐个执行 `call_list`。
  - `plugins/life_engine/tools.py:1064` 子代理工具循环也是逐个执行。

结论：现在不是“模型不会并行”，而是“框架没有统一的并行工具调度器”。

### 2. 搜索链路还是 Python 扫目录，性能模型偏弱

- `plugins/life_engine/grep_tools.py:194` 用 `os.walk()` 递归遍历。
- `plugins/life_engine/grep_tools.py:85` 直接 `read_text()` 读整个文件，再 `splitlines()` 逐行匹配。
- `plugins/life_engine/tools.py:761` 的 `nucleus_list_files` 也基于 `iterdir()` 递归构树。

这套实现的主要问题：

1. 搜索速度和文件数强相关，项目一大就会慢。
2. 每个文件都拉到 Python 里做正则，CPU 和内存都不划算。
3. 没吃到 `ripgrep` 的忽略规则、二进制跳过、流式扫描、并行搜索这些现成能力。
4. 现在的 `grep` 和 `list` 工具语义有些重叠，模型容易先 `list` 再 `grep`，多走弯路。

### 3. 代码里已经有可复用的并发/缓存基础设施

- `src/kernel/concurrency/task_manager.py:269` 已有统一的 `gather()`。
- `src/core/managers/tool_manager/tool_use.py:49` 已有工具缓存开关。
- `src/core/managers/tool_manager/tool_history.py:143` 已有 TTL 缓存能力。
- `src/core/config/model_config.py:188` 已有 `concurrency_count` 字段，但目前没有被真正用到。

结论：不用从零造基础设施，重点是做一个统一的工具执行规划层。

### 4. 代码里有“想并行但还没并行”的信号

- `plugins/life_engine/memory_service.py:664` 注释写的是“并行执行关键词和语义检索”。
- 但 `plugins/life_engine/memory_service.py:665-666` 实际是顺序 `await`。

这说明整个项目已经在概念上接受并行，只是还没收敛成统一实现。

## Claude Code 参考结论

我只参考了官方文档里的稳定原则，不照搬非公开实现细节。

### 1. Claude Code 的核心不是“一个大而全工具”，而是低层能力拆清楚

官方文档把 Claude Code描述为一个 agentic coding tool，围绕读文件、搜索、改文件、执行命令、委派子任务来工作。

这对 Neo-MoFox 的启发是：

1. 不要让一个工具同时承担“找文件 + 搜内容 + 浏览结构 + 读详情”。
2. 应该把工具拆成更清晰的几层：`glob/list`、`grep/search`、`read`、`edit/write`、`task/agent`。

参考：

- https://code.claude.com/docs/en/how-claude-code-works

### 2. Claude Code 明确鼓励把独立工作拆给子代理并行完成

官方文档明确提到可以使用 sub-agents 处理独立任务，以并行化工作。

这对 Neo-MoFox 的启发是：

1. 并行不只是在“一轮里同时跑多个读工具”。
2. 更高一级的并行，是把多个互不依赖的子任务拆给独立 agent。
3. 因此我们的设计应该同时覆盖：
   - 同轮多工具并行
   - 多子任务并行

参考：

- https://docs.anthropic.com/en/docs/claude-code/common-workflows

### 3. Claude Code 官方排障文档直接建议优先用系统 ripgrep

官方排障文档专门提到搜索问题时，建议安装系统 `ripgrep`，并优先使用系统版本而不是内建版本。

这对 Neo-MoFox 的启发非常直接：

1. 文件搜索不该再以 Python `os.walk + read_text + re` 为主路径。
2. 主路径应该切换为系统 `rg`。
3. Python 版本只保留为 fallback。

参考：

- https://docs.anthropic.com/en/docs/claude-code/troubleshooting

## 设计原则

### 原则 1：并行只先开放给“安全的只读工具”

第一阶段不要追求所有工具都并行。只把这些工具纳入并行池：

- 文件内容搜索
- 文件列表/Glob
- 文件信息读取
- 文件内容读取
- 记忆查询

先不并行这些工具：

- 写文件
- 编辑文件
- 删除/移动文件
- 会发消息的 action
- 会修改状态的 TODO/记忆写入工具

原因很简单：只读并行几乎只有性能收益，写操作并行则会立刻引入锁、顺序、幂等、冲突恢复问题。

### 原则 2：并行调度必须集中在一层，不能散落在各个 Chatter/Plugin

现在 `default_chatter`、`life_engine heartbeat`、`nucleus_run_agent` 都各自维护一套“看到 `call_list` 然后挨个执行”的逻辑。

这会导致：

1. 每改一次并行策略，要改三四处。
2. 去重、缓存、超时、日志格式都不统一。
3. 有的链路并行，有的链路仍旧串行，行为会漂。

所以必须引入统一执行器，而不是继续在各处手搓循环。

### 原则 3：高性能搜索要优先解决“搜索”，不是先做索引系统

最先该做的是：

1. 用 `rg` 替换 Python grep。
2. 用 `fd` 或 `rg --files` 替换递归列目录的重路径。
3. 规范 glob、忽略规则、结果截断、上下文输出。

不要一上来就搞常驻索引服务。那是第二阶段以后再考虑的事。

## 目标架构

## 一、统一工具执行规划层

建议新增一层统一执行器，例如：

- `src/core/managers/tool_manager/execution_profile.py`
- `src/core/managers/tool_manager/execution_planner.py`
- `src/core/managers/tool_manager/parallel_executor.py`

### 1. 给 BaseTool 增加执行元数据

在 `src/core/components/base/tool.py` 为工具增加类级元数据，例如：

```python
execution_mode = "read_only"      # read_only | write | action | agent | external
parallel_safe = True
cache_ttl_seconds = 0
mutex_key = ""
timeout_seconds = 30
max_concurrency = 8
```

首批建议：

- `nucleus_read_file` => `read_only`
- `nucleus_file_info` => `read_only`
- `nucleus_list_files` => `read_only`
- `nucleus_grep_file` => `read_only`
- `nucleus_search_memory` => `read_only`
- `nucleus_write_file/edit_file/delete_file/move_file` => `write`
- `nucleus_run_agent` => `agent`

### 2. 新增 ToolExecutionPlanner

输入：

- `call_list`
- `ToolRegistry`
- 当前触发消息/上下文

输出：

- 一个执行计划 `ExecutionPlan`

执行计划至少做这几件事：

1. 同轮去重
2. 命中缓存的直接回填
3. 把可并行的只读工具分组
4. 把写工具和 agent 工具放到串行或单独阶段
5. 保留原始顺序索引，最后按原顺序回填 `TOOL_RESULT`

这里“按原顺序回填”很重要。  
`src/kernel/llm/context.py:121-154` 要求 tool_result 必须完整覆盖 tool_call。当前校验不强制结果顺序完全一致，但统一按原顺序回填更稳、更易调试。

### 3. 新增 ParallelToolExecutor

执行语义建议：

1. 同轮内先跑所有命中缓存的 read-only 调用。
2. 剩余 read-only 调用按组并发执行。
3. `write`/`action`/`agent` 类型默认串行执行。
4. 每个任务都有独立 timeout。
5. 失败只影响本 call，不导致整轮全挂。
6. 所有结果统一汇总后一次性追加到 response。

建议直接复用 `src/kernel/concurrency/task_manager.py:269` 的 `gather()`，不要再散落使用裸 `asyncio.gather()`。

### 4. 替换现有三条主调用链

把下面这些串行循环统一替换成执行规划层：

- `plugins/default_chatter/tool_flow.py`
- `plugins/life_engine/service.py`
- `plugins/life_engine/tools.py` 中的 `nucleus_run_agent`

这一步是整个改造的核心。  
不做这一层，模型就算返回了多个 tool call，也只是“看起来支持并行”，实际仍然是串行。

## 二、搜索栈升级为 rg/fd 优先

### 1. 改造 `nucleus_grep_file`

当前实现位置：

- `plugins/life_engine/grep_tools.py`

建议主路径改成：

```bash
rg --json --line-number --column --smart-case \
  --max-count <N> \
  --max-filesize 1M \
  -g <glob> \
  <pattern> <search_root>
```

实现建议：

1. 用 `asyncio.create_subprocess_exec()` 启动 `rg`。
2. 解析 `--json` 输出，而不是解析纯文本。
3. 对 `content` 模式保留上下文行支持，可用 `-C` 或自行拼接邻近行。
4. workspace 路径校验仍保留在 Python 层。
5. 如果系统没有 `rg`，再 fallback 到现有 Python 实现。

这样做的收益：

1. 大目录下搜索速度会明显提升。
2. 二进制/大文件/ignore 处理更可靠。
3. 可自然支持列号、文件级统计、截断。

### 2. 新增 `nucleus_glob_files`

Claude Code 风格里，`Glob` 和 `Grep` 通常是分开的。  
你现在只有 `list_files` 和 `grep_file`，缺一个“按模式快速找路径”的轻量工具。

建议新增：

- `nucleus_glob_files(pattern: str, path: str = "", limit: int = 200)`

优先实现：

1. 有 `fd` 时用 `fd`。
2. 没有 `fd` 时用 `rg --files` + Python 过滤。
3. 再不行才用 `Path.rglob()`。

这样模型在“不知道精确路径，只知道名字模式”时，不必先跑重型 `list_files`。

### 3. 轻量化 `nucleus_list_files`

`list_files` 应该退回“结构浏览工具”，而不是“万能搜路径工具”。

建议：

1. 非递归模式保留 Python `iterdir()`，因为这是小目录浏览，足够便宜。
2. 递归模式改为 `rg --files` 或 `fd`，再按层级重建树。
3. 默认结果上限要更严格，比如 200-500 项，否则目录一大就会压爆上下文。

### 4. 明确三类工具职责

- `glob_files`：按文件名/路径模式找文件
- `grep_file`：按内容/正则找文件或匹配行
- `read_file`：读取已知路径内容

把职责拆清楚后，模型的工具规划会更接近 Claude Code 的搜索路径。

## 三、并行子代理能力

### 1. 现有 `nucleus_run_agent` 先统一到共享执行器

当前 `plugins/life_engine/tools.py:1047-1091` 里，子代理自己的工具循环也是手写串行版。  
第一步不需要立刻引入新工具，先让它也复用共享执行器。

### 2. 第二阶段再加显式并行子任务工具

建议新增一个更明确的工具，而不是让模型自己“同时调多次 `nucleus_run_agent`”：

- `nucleus_parallel_agents(tasks: list[AgentTask], max_parallel: int = 3)`

`AgentTask` 可以包含：

- `task`
- `context`
- `expected_output`
- `write_scope`

执行规则：

1. 默认只允许只读/不同写域的子任务并行。
2. 相同 `write_scope` 的任务不并行。
3. 每个子任务单独返回摘要。
4. 父代理只拿摘要，不直接拿完整上下文。

这会更接近 Claude Code 文档里“把独立工作拆给 sub-agents 并行完成”的能力。

## 四、缓存与预算控制

### 1. 缓存不要全局打开，而是按工具元数据启用

`ToolUse` 已经有缓存雏形，但现在不是策略化使用。

建议：

1. 只对 `read_only` 工具启用缓存。
2. 缓存 key 要去掉 `reason`。
3. `grep`、`glob`、`file_info` 可以给短 TTL。
4. `read_file` 只有在文件 `mtime` 未变化时才命中。

### 2. 增加工具预算而不是只看轮数

现在 life_engine 主要靠 `max_rounds_per_heartbeat` 控制。  
这太粗。

建议增加这些预算：

- `max_parallel_tool_calls_per_round`
- `max_total_tool_calls_per_turn`
- `max_search_results_per_call`
- `max_agent_fanout`
- `max_total_tool_wall_time_seconds`

这样比只卡“轮数”更像 Claude Code 式的工程化调度。

## 五、可观测性

建议补这些指标：

1. 每轮工具调用数
2. 每轮并发峰值
3. 每个工具耗时 P50/P95
4. cache hit rate
5. 搜索工具返回量与截断率
6. 因锁/互斥降级成串行的次数

日志建议统一记录：

- `round_id`
- `tool_name`
- `call_id`
- `execution_mode`
- `cache_hit`
- `latency_ms`
- `queue_wait_ms`
- `parallel_group_id`

## 分阶段落地方案

## Phase 0：收口设计，不改行为

目标：

1. 给 `BaseTool` 增加执行元数据字段。
2. 新增共享执行器模块骨架。
3. 不改变现有串行行为。

产出：

- 统一元数据模型
- 单元测试骨架

## Phase 1：统一执行器接管所有 call_list

目标：

1. `default_chatter` 改成经过 `ToolExecutionPlanner`。
2. `life_engine heartbeat` 改成经过 `ToolExecutionPlanner`。
3. `nucleus_run_agent` 改成经过 `ToolExecutionPlanner`。

此阶段仍可配置为“并行关闭”，只做代码收口。

## Phase 2：打开只读工具并行

目标：

1. 打开 `read_only` 工具并行执行。
2. 写工具继续串行。
3. 保证回填顺序稳定。

验收标准：

1. 一轮返回多个只读 tool calls 时， wall time 明显低于串行总和。
2. `context.validate_for_send()` 不出现 tool_result 缺失或重复。

## Phase 3：搜索栈切到 rg/fd

目标：

1. `nucleus_grep_file` 主路径使用 `rg`。
2. 新增 `nucleus_glob_files`。
3. `nucleus_list_files` 只保留结构浏览职责。

验收标准：

1. 中大型 workspace 下，关键词搜索耗时显著下降。
2. 工具输出结构对上层保持兼容，至少保留 `files_with_matches`/`content` 两种模式。

## Phase 4：并行子代理

目标：

1. `nucleus_run_agent` 内部支持并行只读工具。
2. 新增显式并行子任务工具。

验收标准：

1. 可把多个独立检索/分析任务同时分发。
2. 不同写域任务可安全并行，相同写域自动串行。

## 建议改动文件

核心改动：

- `src/core/components/base/tool.py`
- `src/core/managers/tool_manager/tool_use.py`
- `src/core/managers/tool_manager/tool_history.py`
- `src/core/managers/tool_manager/` 下新增执行规划模块
- `src/core/components/base/chatter.py`
- `plugins/default_chatter/tool_flow.py`
- `plugins/life_engine/service.py`
- `plugins/life_engine/tools.py`
- `plugins/life_engine/grep_tools.py`
- `plugins/life_engine/config.py`

可能需要同步：

- `config/model.toml`
- `src/core/config/model_config.py`
- `src/kernel/llm/request_inspector.py`

## 我建议的实施顺序

如果现在就开工，我建议按这个顺序做：

1. 先做统一执行器，不碰搜索。
2. 把三条串行 call_list 链路全部接到统一执行器。
3. 只开放只读工具并行。
4. 再把 `grep` 切到 `rg`。
5. 最后再做显式并行子代理。

原因：

1. 并行调度是主干，先做它，后面所有工具都能吃到收益。
2. `rg` 改造虽然收益大，但只覆盖搜索，不解决整个系统的工具编排问题。
3. 并行子代理收益高，但复杂度也最高，应该放到最后。

## 风险点

### 1. 写工具误并行

如果没有元数据和互斥控制，文件编辑/删除/移动会非常危险。  
因此第一阶段必须硬编码“只有 read-only 可以并行”。

### 2. 搜索输出结构变化导致上层提示词漂移

`nucleus_grep_file` 如果直接改成 `rg` 文本输出，模型体验会退化。  
必须在 Python 层把 `rg --json` 转成和当前近似兼容的结构化结果。

### 3. 子代理并行带来的资源放大

多个子代理会同时占用模型配额、搜索进程、磁盘 IO。  
必须有 `max_agent_fanout` 和全局 semaphore。

## 最终建议

这次改造不要理解成“把 grep 写快一点”。  
真正该做的是把整个工具调用系统升级成三层：

1. `Tool schema / execution profile`
2. `Execution planner / parallel executor`
3. `High-performance search backend (rg/fd)`

只要这三层站稳，你的系统就会从“模型会调工具”变成“模型在一个工程化的工具运行时里工作”。这才是更接近 Claude Code 的改造方向。

## 参考资料

- Claude Code: How Claude Code works  
  https://code.claude.com/docs/en/how-claude-code-works
- Claude Code: Common workflows  
  https://docs.anthropic.com/en/docs/claude-code/common-workflows
- Claude Code: Troubleshooting  
  https://docs.anthropic.com/en/docs/claude-code/troubleshooting
