# 第 4 章 · 工程系统总览：app / core / kernel 与 life_engine 插件

> *"Neo-MoFox-Soul 的关键不是又写了一个聊天插件，而是把插件、消息、LLM、调度、存储和后台心跳组织成一个能长期运行的系统。"*

---

## 4.0 为什么先讲工程总览

前几章已经说明课程定位和三条工程约束。本章开始直接落到实现：Neo-MoFox-Soul 的主体不是一个单文件 Bot，也不是单次 LLM 调用，而是由 `src/app`、`src/core`、`src/kernel` 和 `plugins/life_engine` 共同组成的运行系统。

图 F0 已在导论中给出总览。本章按同一结构展开：先讲框架三层，再讲 Life Engine 插件如何挂载，最后讲消息、状态和心跳如何形成闭环。

## 4.1 `app` 层：运行时装配与生命周期

`src/app` 是最靠近启动入口的一层，职责是**编排生命周期**，而不是重复实现底层能力。

核心对象是 `src/app/runtime/bot.py` 中的 `Bot`。它维护的运行态可以拆成四组：

- **生命周期标志**：`_initialized`、`_running`、`_shutdown_requested` 标记初始化、运行和关闭流程。
- **Kernel 基础设施实例**：`config`、`logger`、`event_bus`、`task_manager` 在启动早期完成初始化，供后续组件复用。
- **Core 运行组件**：`message_receiver`、`sink_manager`、`plugin_loader`、`plugin_manager` 负责消息入口、发送出口、插件发现和插件管理。
- **插件加载账本**：`load_order`、`manifests`、`load_results` 记录插件发现、依赖排序和加载结果。

启动链路可以概括为：

```text
main.py
  -> Bot.start()
  -> Bot.initialize()
  -> _initialize_kernel()
  -> _initialize_core()
  -> _discover_plugins()
  -> _install_plugin_deps()
  -> _load_plugins()
  -> Bot.run()
```

这条链路对课程报告很重要：它说明 Life Engine 不是一个孤立后台脚本，而是在框架生命周期中被发现、加载、启动和关闭的插件。

`src/app/plugin_system` 还提供面向插件作者的稳定入口，包括 base、api、types 三类导出。插件应优先依赖这些入口，而不是直接穿透到深层内部实现。

## 4.2 `core` 层：组件模型、管理器与消息框架

`src/core` 是 Neo-MoFox-Soul 的领域框架层，负责把插件能力统一成系统组件。它主要包含五类能力：

- **组件模型**（`components/`）：定义 Plugin、Action、Tool、Agent、Chatter、Service、Router 等组件基类与注册语义。
- **管理器**（`managers/`）：管理插件、Action、Chatter、Command、Router、Service、Stream、Permission 等运行时对象。
- **消息与会话模型**（`models/`）：定义 Message、ChatStream、LLMUsage 等领域数据结构。
- **传输链路**（`transport/`）：处理消息接收、分发、Sink、HTTP router 和适配器协作。
- **Prompt 能力**（`prompt/`）：管理系统提示词、模板、渲染策略和 system reminder。

这层回答的是“一个插件注册了组件之后，系统如何知道它能做什么”。例如 LifeChatter 能成为对话器，是因为它实现 `BaseChatter` 并被 ChatterManager 管理；`life_send_text` 能成为行动，是因为它作为 Action 进入 ActionManager 的 schema 与执行链路。

## 4.3 `kernel` 层：基础设施底座

`src/kernel` 提供与业务含义无关但系统必须依赖的基础设施：

- **配置**（`config/`）：加载核心配置与模型配置。
- **日志**（`logger/`）：统一日志、控制台输出和文件记录。
- **事件**（`event/`）：发布运行时事件，支撑跨模块观测；core 层的 EventManager 在其上注册插件事件处理器。
- **并发**（`concurrency/`）：管理异步任务、看门狗和任务信息。
- **调度**（`scheduler/`）：提供定时任务与延迟触发能力。
- **数据库**（`db/`）：支撑 ChatStream、消息、LLMUsage 等结构化数据。
- **存储**（`storage/`）：提供 JSON 状态存储。
- **向量库**（`vector_db/`）：作为记忆检索与语义索引底座。
- **LLM**（`llm/`）：封装请求、payload、工具调用、模型客户端和监控。

`kernel` 层不直接关心“主意识”或“潜意识”。它的价值在于把配置、日志、事件、调度、数据库、向量库和 LLM 调用变成可复用能力，让 Life Engine 插件可以专注于 Agent 行为本身。

## 4.4 `life_engine` 插件：连续 Agent 的主要实现域

`plugins/life_engine/core/plugin.py` 中的 `LifeEnginePlugin` 是本报告最关键的工程入口。它通过 `get_components()` 向框架注册多类组件：

- `LifeEngineService`：后台心跳和内部状态推进服务；
- `LifeEngineMessageCollectorHandler`：收集外部消息并转成事件；
- `LifeEngineCommandHandler`：提供命令入口；
- SNN / Memory / Dream / Monitor 等 Router；
- 文件、搜索、网页、日程、记忆、事件检索等 Tool；
- 当配置启用 chatter 时，注册 `LifeChatter` 及其专用 Action。

这说明 Life Engine 不是绕过框架运行的旁路系统，而是一个标准插件。它的对外表达、后台心跳、可观测性路由和工具能力都通过 core 的组件系统进入运行时。

## 4.5 插件启动链路

Life Engine 的启动发生在插件生命周期中：

```text
Bot._load_plugins()
  -> PluginManager 加载 life_engine
  -> LifeEnginePlugin.get_components()
  -> 注册 Service / Chatter / Action / Tool / Router
  -> LifeEnginePlugin.on_plugin_loaded()
  -> setup_life_audit_logger()
  -> LifeEngineService.start()
```

如果 `settings.enabled = false`，插件会清理运行态上下文并跳过心跳。否则，`LifeEngineService.start()` 会启动后台运行逻辑，使系统在没有用户消息时仍然保持状态推进。

这条链路把“连续存在”从概念落到了可检查的启动过程：只要插件加载成功且配置启用，后台服务就会进入框架管理的生命周期。

## 4.6 运行时数据流：从消息到状态再到回复

一次典型外部消息会经过如下链路：

```text
Adapter / HTTP / User Message
  -> core transport
  -> MessageReceiver / SinkManager / StreamManager
  -> LifeEngineMessageCollectorHandler
  -> Life Engine event stream
  -> runtime snapshot / memory / ThoughtStream
  -> LifeChatter context assembly
  -> kernel.llm request
  -> Action / Tool execution
  -> message send / inner monologue / event write-back
```

这个链路包含两个闭环：

1. **外部行动闭环**：用户消息进入系统，LifeChatter 生成回复或工具调用，结果再写回消息流。
2. **内部状态闭环**：外部事件进入 Life Engine，改变记忆、调质、SNN、ThoughtStream 和运行态快照，后续又影响 LifeChatter 的上下文。

![Figure F5 · 数据流时序图](/root/Elysia/Neo-MoFox/Report/04_figures/F5_dataflow_timing.svg)

*Figure F5 · 从外部消息到 LifeChatter、Life Engine、调质、记忆、思考流，再回写到事件流的典型闭环时序。*

因此，Neo-MoFox-Soul 的对话不是单纯“历史消息 + LLM”，而是“消息框架 + 插件事件流 + 后台状态 + LLM 行动控制”的组合。

## 4.7 LifeChatter + Life Engine：双意识异步运行

在上述框架之上，当前 Neo-MoFox-Soul 使用 **LifeChatter + Life Engine** 作为核心运行范式。

![Figure F4 · 双意识异步运行：LifeChatter + Life Engine](/root/Elysia/Neo-MoFox/Report/04_figures/F4_dual_consciousness_async.png)

*Figure F4 · LifeChatter 以用户消息节律运行，Life Engine 以心跳节律运行；二者通过内心独白、状态快照、ThoughtStream 与梦境残影异步同步。*

三个相位的工程职责如下：

- **社交主意识**：`LifeChatter` 由用户消息、主动机会或续话机会触发，负责对话表达、工具调用、行动输出和内心独白记录。
- **潜意识**：`LifeEngineService` 由 30 秒心跳、睡眠窗口和事件流变化触发，负责状态推进、记忆巩固、SNN/调质更新和 ThoughtStream 管理。
- **注意力桥**：`ThoughtStreamManager` 与 runtime snapshot 通过 revision / high-water 同步，把潜意识持续在意的问题同步给主意识。

这里的“双意识”是工程分层术语，不是主观体验断言。它表达的是两个不同运行节律：LifeChatter 在需要表达时工作，Life Engine 在后台持续推进。

因此，本报告不再把早期 DFC/default_chatter 作为当前架构核心。DFC 属于旧时代的对话桥接形态；当前架构以 `life_chatter` 和 `life_engine` 插件内同步为准。

## 4.8 Life Engine 内部子系统

Life Engine 插件内部又包含多类仿生子系统。它们不是单独解决问题，而是共同支撑连续 Agent。

![Figure F1 · LifeChatter + Life Engine 三层系统总览](/root/Elysia/Neo-MoFox/Report/04_figures/F1_lifechatter_three_layer.png)

*Figure F1 · LifeChatter 主意识、Life Engine 潜意识与底层仿生子系统的三层关系。图为工程架构示意，不表示真实脑区等价。*

主要子系统可以按职责理解：

- **SNN**（`plugins/life_engine/snn/`）：负责快速事件响应、本地可塑性和 drive 输出。
- **Neuromod**（`plugins/life_engine/neuromod/`）：负责慢变量调节、昼夜节律和习惯稳态。
- **Memory**（`plugins/life_engine/memory/`）：负责记忆图、检索、衰减和 Hebbian 边强化。
- **Dream**（`plugins/life_engine/dream/`）：负责离线巩固、梦境残影和经验重组。
- **ThoughtStream**（`plugins/life_engine/streams/`）：负责中期注意力线索、焦点同步和背景在意。
- **Tools / Agents**（`plugins/life_engine/tools/`、`agents/`）：负责文件、搜索、网页、日程和复杂任务代理。

在运行时，这些子系统的输出被压缩为 LifeChatter 可读取的上下文：记忆提示、运行态摘要、梦境残影、内心独白和 ThoughtStream 焦点。

## 4.9 多时间尺度与持久化

Neo-MoFox-Soul 的连续性来自多时间尺度并行运行：

- **秒级**：SNN 神经元活动度和 drive 通过 LIF step、decay-only 和 EMA 演化，典型触发是 SNN tick。
- **30 秒级**：Life Engine 心跳状态通过事件采样、状态推进和持久化演化，典型触发是 heartbeat。
- **分钟到小时级**：调质浓度通过 ODE 回归与刺激项演化，典型触发是 heartbeat / circadian。
- **小时到天级**：ThoughtStream 焦点与好奇心通过 advance、lazy decay、revision sync 演化，典型触发是内心独白或事件触发。
- **天级**：习惯 streak / strength 通过累加和阈值衰退演化，典型触发是每日事件。
- **周到月级**：记忆图边权重通过 Hebbian 强化和 Ebbinghaus 衰减演化，典型触发是检索或做梦。
- **永久尺度**：状态通过 JSON、SQLite 和索引写盘持久化，典型触发是心跳末尾。

系统状态可以粗略拆成：

```text
system_state(t) = (
  snn_state,
  modulator_state,
  memory_state,
  thought_stream_state,
  chatter_state
)
```

Neo-MoFox-Soul 追求的连续性不是“每一层每一秒都改变”，而是：在外部输入间隙中，至少有一个内部状态分量会随时间推进，并在后续表达、检索或持久化记录中留下可检查的变化。

## 4.10 小结

本章把报告重心提前放到了工程结构上：`app` 负责运行时装配，`core` 负责组件与消息框架，`kernel` 提供基础设施，`life_engine` 插件实现连续 Agent 的主要行为。后续章节再分别展开 SNN、调质、记忆、做梦、心跳、同步和 LifeChatter 执行细节。
