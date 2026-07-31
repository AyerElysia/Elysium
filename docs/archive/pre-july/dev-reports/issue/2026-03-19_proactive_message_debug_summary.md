# 2026-03-19 主动消息 / 事件系统排查总结

## 现象
本轮主要排查的是 `proactive_message_plugin` 无法触发“内心独白”的问题，同时顺手修复了事件处理器与配置相关的一系列连锁问题。

用户观察到的典型现象包括：
- `event_manager | ERROR | ... 执行失败: CONTINUE`
- `wait_longer` 被日志标记为 `chatter 不匹配`
- 明明进入了 `Wait` 状态，却一直看不到“开始等待计时 / 检查超时 / 内心独白”
- 调度任务创建后又出现 `coroutine was never awaited`

---

## 问题 1：事件处理器返回了不存在的枚举值 `CONTINUE`

### 现象
日志出现：

```text
事件处理器 time_awareness_plugin:event_handler:time_tracker 执行失败: CONTINUE
事件处理器 proactive_message_plugin:event_handler:on_message 执行失败: CONTINUE
```

### 根因
事件系统的 `EventDecision` 只有：
- `SUCCESS`
- `STOP`
- `PASS`

并没有 `CONTINUE`。

但 `time_awareness_plugin` 与 `proactive_message_plugin` 的事件处理器里写了 `EventDecision.CONTINUE`，所以运行时直接抛异常。

### 修复
将这两个插件中的返回值统一改为 `EventDecision.SUCCESS`。

### 经验
- 事件处理器返回值必须严格匹配 `src/kernel/event/core.py` 中的枚举定义。
- 如果日志里出现“执行失败: CONTINUE”这种很怪的文本，通常不是业务报错，而是错误地引用了不存在的枚举成员。

---

## 问题 2：配置文件语法错误导致插件加载失败

### 现象
用户修改配置时，`config.toml` 中残留了非法字符，例如：

```toml
enabled = true【
```

### 根因
TOML 解析失败，插件虽然被发现，但初始化时加载配置报错，最终进入“已初始化但部分插件失败”的状态。

### 修复
移除非法字符，恢复为合法 TOML：

```toml
enabled = true
```

涉及：
- `config/plugins/time_awareness_plugin/config.toml`
- `config/plugins/proactive_message_plugin/config.toml`

### 经验
- 配置文件里哪怕只多一个全角符号，也会导致整个插件失效。
- 看见“加载了 X/Y 个插件（N 个失败）”时，优先排查配置语法，再排查代码。

---

## 问题 3：`wait_longer` 被 `default_chatter` 过滤

### 现象
日志出现：

```text
移除组件: proactive_message_plugin:tool:wait_longer(chatter 不匹配（允许: proactive_message_plugin）)
```

### 根因
`wait_longer` 工具原本只允许 `proactive_message_plugin` 使用：

```python
chatter_allow = ["proactive_message_plugin"]
```

而实际运行的是 `default_chatter`，所以被框架过滤掉。

### 修复
将其放宽为：

```python
chatter_allow = ["proactive_message_plugin", "default_chatter"]
```

### 经验
- 看到“移除组件”不一定是错误，很多时候只是 `chatter_allow` / `chat_type` / `platform` 过滤逻辑在生效。
- 真正的问题在于：这个工具是不是本来就应该给当前 chatter 用。

---

## 问题 4：主动消息插件的最小等待时间被硬编码为 5 分钟

### 现象
即使 TOML 里配置：

```toml
first_check_minutes = 0.1
```

日志依然显示接近 5 分钟后才可能触发，或者根本看不到短时间触发结果。

### 根因
`proactive_message_plugin/service.py` 里把最小等待时间写死成了：

```python
min_wait = 5.0
wait_minutes = max(wait_minutes, min_wait)
```

并没有读取配置中的 `min_wait_interval_minutes`，也没有尊重调用方传入的 `first_check_minutes`。

### 修复
改为只保留极小下限防止 0 触发：

```python
wait_minutes = max(wait_minutes, 0.01)
```

### 经验
- 如果一个配置“改了完全没效果”，优先怀疑：代码里是否有硬编码覆盖。
- 配置项写在 TOML 中，不代表业务逻辑真的用了它。

---

## 问题 5：`ON_CHATTER_STEP` 只在执行前发布，没把 `Wait` 结果通知给插件

### 现象
日志里能看到：
- Chatter 进入 `Wait` 状态
- 但主动消息插件始终没有“开始等待计时”

### 根因
`proactive_message_plugin` 依赖 `ON_CHATTER_STEP` 的 `result` 参数来判断：

- 如果 `result` 是 `Wait`
- 就开始调度内心独白等待任务

但原来的 `loop.py` 只在执行 `anext(chatter_gene)` 之前发布了一次 `ON_CHATTER_STEP`，那时还没有 `result`。
执行完成之后没有再次发事件，所以插件永远拿不到 `Wait`。

### 修复
在：

```python
result = await anext(chatter_gene)
```

之后补发一次 `ON_CHATTER_STEP`，并带上：

```python
{"result": result}
```

### 经验
- 某插件“监听了事件但完全无反应”，要检查的不只是“有没有发事件”，还要检查**事件是在正确时机发的**。
- 对于依赖执行结果的插件，必须在结果产生后再发一次事件。

---

## 问题 6：主动消息插件调用了不存在的 `StreamManager.get_stream()`

### 现象
日志出现：

```text
主动消息插件 | DEBUG | 处理 Chatter Wait 状态失败：'StreamManager' object has no attribute 'get_stream'
```

### 根因
插件里写的是：

```python
sm = get_stream_manager()
chat_stream = await sm.get_stream(stream_id)
```

但 `StreamManager` 本体并没有 `get_stream()` 这个方法。
可用的是 `stream_api.py` 暴露的：

```python
from src.app.plugin_system.api.stream_api import get_stream
```

### 修复
将两处取流逻辑统一改成使用 `stream_api.get_stream()`：
- `_on_chatter_step()`
- `_on_check_timeout()`

### 经验
- 不要混淆“manager 内部方法”和“对外 API 封装”。
- 框架里很多对象只有 `get_or_create_stream()`，不一定有 `get_stream()`。

---

## 问题 7：调度器回调传入了同步 lambda，返回 coroutine 但未 await

### 现象
日志出现：

```text
RuntimeWarning: coroutine 'ProactiveMessagePlugin._on_check_timeout' was never awaited
```

### 根因
代码原本写成：

```python
callback=lambda: self._on_check_timeout(stream_id)
```

这个 `lambda` 是同步函数，调用后只是返回一个 coroutine 对象。
而调度器判断逻辑是：
- 如果是 `async def`，就 `await`
- 否则就当同步函数跑在线程里

于是该 coroutine 对象被创建了，但没人 await，最终出现 warning。

### 修复
改成真正的异步回调：

```python
async def _timeout_callback() -> None:
    await self._on_check_timeout(stream_id)
```

再把它传给 scheduler。

### 经验
- `lambda: async_func()` 不是异步回调，只是“返回协程对象的同步函数”。
- 如果调度器 / 框架要判断是不是协程函数，必须传 `async def` 定义的函数本身。

---

## 问题 8：内心独白错误地从 `CoreConfig` 读取模型配置

### 现象
日志出现：

```text
内心独白 | ERROR | 生成内心独白失败：'CoreConfig' object has no attribute 'models'
```

### 根因
内心独白代码中写的是：

```python
from src.core.config import get_core_config
config = get_core_config()
model_config = config.models.get(model_set)
```

但当前项目里：
- `CoreConfig` 不负责保存模型表
- 模型配置属于 `ModelConfig`
- 正确入口应当是 `llm_api.get_model_set_by_task()` 或 `get_model_config().get_task()`

### 修复
将内心独白改为通过当前框架标准接口读取模型任务：

```python
model_config = llm_api.get_model_set_by_task(model_set)
```

并使用 `llm_api.create_llm_request(...)` 创建请求。

### 经验
- `core_config` 和 `model_config` 在当前架构里是两套配置，不能混用。
- 如果报错里出现 `CoreConfig has no attribute ...`，优先检查是不是把模型配置当成核心配置用了。

---

## 问题 9：内心独白的工具注册不完整/不符合当前框架调用方式

### 现象
即使内心独白模型调用成功，仍可能出现：
- 不返回任何工具调用
- 只能继续等待，不能主动发消息
- payload 格式不符合框架习惯

### 根因
旧代码存在几个问题：

1. 没有按当前框架方式创建请求，而是直接手写 `LLMRequest`
2. `USER` payload 写法不标准：

```python
LLMPayload(ROLE.USER, Text(prompt_text))
```

而当前框架统一用列表内容：

```python
LLMPayload(ROLE.USER, [Text(prompt_text)])
```

3. prompt 明明要求模型二选一：
   - 调用 `send_text`
   - 调用 `wait_longer`

   但实际只注册了 `wait_longer`，没有注册 `send_text`

### 修复
- 改为使用 `llm_api.create_llm_request(...)`
- 改为标准 payload：`[Text(prompt_text)]`
- 工具注册改为：

```python
tool_registry = llm_api.create_tool_registry([SendTextAction, WaitLongerTool])
```

- 同时兼容解析：
  - `send_text` / `action-send_text`
  - `wait_longer` / `tool-wait_longer`

### 经验
- Prompt 里要求模型调用什么工具，就必须真的把对应工具注册进去。
- 当前框架里 action/tool 名称可能带前缀，解析返回结果时要做兼容。
- 能跑通一次请求，不代表工具链完整，工具 schema 和 payload 结构同样关键。

---

## 问题 10：内心独白默认只打印日志，不会进入后续上下文

### 现象
即使已经成功打印了：

```text
内心独白内容：......
```

下一次内心独白或下一轮 DFC 回复，仍然看不到“自己上一次想了什么”。

### 根因
原实现只是：
- 调用模型
- 记录日志
- 按决策继续等待或主动发消息

但并没有把 `thought` 写回当前 `ChatStream.context.history_messages`。

### 修复
在 `_handle_decision()` 开始处，先把 `result.thought` 注入当前 stream 的历史消息：

```python
await self._inject_inner_monologue(chat_stream, result.thought)
```

并构造一条带有 `is_inner_monologue=True` 的 `Message` 写入：

```python
chat_stream.context.add_history_message(message)
```

### 经验
- 仅有日志可见性，不等于上下文可记忆性。
- 如果希望“下一次能看到上一次内心活动”，就必须把独白写回上下文或记忆层。
- 对于这类“自我状态”信息，最简单直接的做法就是作为 bot 历史消息写入当前 stream。

---

## 问题 11：主动发消息错误调用 `ChatStream.get_bot_info()`

### 现象
日志出现：

```text
发送主动消息失败：'ChatStream' object has no attribute 'get_bot_info'
```

### 根因
主动消息发送逻辑里写了：

```python
bot_info = await chat_stream.get_bot_info()
```

但 `ChatStream` 并没有这个方法。
它本身已经在创建时保存了：
- `bot_id`
- `bot_nickname`

### 修复
改为直接使用 `chat_stream` 上已有字段构造消息：

```python
sender_id=chat_stream.bot_id or "bot"
sender_name=chat_stream.bot_nickname or "Bot"
sender_role="bot"
```

### 经验
- `ChatStream` 是运行时数据对象，不是适配器代理，不应假设它具备 adapter 的方法。
- 如果对象上已经有稳定字段，就优先直接读字段，而不是再走一层不存在的查询接口。

---

## 问题 12：内心独白只写入内存上下文，重新激活流后丢失

### 现象
日志能看到内心独白，但下一轮 DFC 问“刚才在想什么”时没有直接引用，像是没写入。

### 根因
内心独白注入仅调用 `chat_stream.context.add_history_message(...)`，未持久化到数据库。`default_chatter` 每轮会 `activate_stream()`，从 DB 重建 `history_messages`，导致内存改动丢失。

### 修复
改为通过 `StreamManager.add_sent_message_to_history(message)` 写入，既更新当前上下文也持久化 DB，后续重建 stream 时仍能读取。

### 经验
- 任何希望跨轮保留的历史，必须走 StreamManager 持久化。
- 仅改内存 context 无法保证下一轮还能看到。

---

## 问题 13：prompt_logger 插件“加载失败/无日志”

### 现象
- 启动时报：插件加载失败 prompt_logger
- IDE 看不到完整提示词日志

### 根因
- manifest 只声明了 event_handler，未声明 service，导致 `PromptLoggerService.get_instance()` 取不到实例，拦截器空转
- 使用原生 logging，IDE 不一定可见
- handler 实际未记录任何 prompt

### 修复
- manifest 增加 service 声明
- logger 改用框架 `get_logger`，请求/响应打印面板 + 文件
- 全局拦截 `LLMRequest.send()`，通用生效

### 经验
- 依赖 service 的插件必须在 manifest 中声明 service，否则组件注册失败但不明显。
- 想要 IDE 可见，统一用框架 logger，而不是裸 logging。

---

## 问题 14：kokoro_flow_chatter 配置非法字符导致加载失败

### 现象
启动时报：`Expected newline or end of document`，插件加载失败。

### 根因
配置文件中写了：

```toml
enabled = false【
```

非法字符破坏 TOML。

### 修复
改回合法行：

```toml
enabled = false
```

### 经验
- TOML 对多余符号极其敏感，任何注释或标记都必须用 `#`，不要插入其他字符。

---

## 当前状态
目前已完成的关键修复：
- 修复 `EventDecision.CONTINUE` 报错
- 修复 `time_awareness_plugin` / `proactive_message_plugin` 配置语法问题
- 放开 `wait_longer` 给 `default_chatter` 使用
- 修复主动消息插件等待时间被硬编码 5 分钟
- 补发 `ON_CHATTER_STEP` 的执行结果事件
- 修复主动消息插件错误调用 `StreamManager.get_stream()`
- 修复 scheduler 回调“协程未 await”问题
- 修复内心独白错误读取 `CoreConfig.models`
- 修复内心独白的请求构造和工具注册不完整问题
- 将内心独白正文写回当前 stream 上下文
- 修复主动发消息错误调用 `ChatStream.get_bot_info()`
- 内心独白改为持久化写入 stream 历史，避免重建上下文后丢失
- prompt_logger manifest/service/输出链路修正，提示词日志恢复
- kokoro_flow_chatter 配置非法字符修正

仍待后续处理：
- `kokoro_flow_chatter` 仍未加载成功，需要单独查首个启动异常
- `thinking_plugin` 日志中仍有：
  ```text
  移除思考触发器失败：'ThinkingPlugin' object has no attribute 'components'
  ```
  这不是当前主线问题，但值得后续修复
- `prefrontal_cortex_chatter` 能力迁移和私聊真实回复能力恢复尚未继续推进

---

## 建议的后续验证步骤
1. 重启后发一条消息，让 `default_chatter` 正常回复并进入 `Wait`。
2. 停止继续发送消息。
3. 在日志中搜索以下关键词：
   - `开始等待计时`
   - `已调度检查任务`
   - `检查超时，触发内心独白`
   - `内心独白无结果`
   - `主动发消息：`
4. 如果仍没有内心独白，优先检查：
   - 是否又有新的异常堆栈
   - 调度器是否真的创建了 `proactive_check_*` 任务
   - 模型调用是否在 `generate_inner_monologue()` 内失败

---

## 本轮最重要的经验总结
- **看到插件加载成功，不等于功能链路通了。**
- **看到事件发布成功，不等于事件时机正确。**
- **看到配置文件存在，不等于业务逻辑真的读取了它。**
- **看到回调被传进 scheduler，不等于它一定会被 await。**
- 这类问题必须沿着完整链路排查：
  - 插件加载
  - 事件注册
  - 事件发布
  - 事件参数
  - 业务分支
  - 调度创建
  - 回调执行
  - 模型调用
  - 最终动作
