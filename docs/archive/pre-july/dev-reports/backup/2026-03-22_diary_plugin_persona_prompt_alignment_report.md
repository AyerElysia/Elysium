# diary_plugin 人设提示词对齐报告

**日期：** 2026-03-22  
**范围：** `plugins/diary_plugin`  
**目标：** 让自动写日记模型与连续记忆压缩模型复用主回复模型的完整人设提示词，并减少“我 / 你”与相似名字混淆导致的人称错误。

---

## 1. 结论

本次改造已完成，且没有破坏 `diary_plugin` 的原有主流程。

现在的行为是：

1. 自动写日记时，会优先复用 `default_chatter` 的完整系统人设提示词。
2. 连续记忆压缩时，也会复用同一份完整人设提示词。
3. 自动写日记与连续记忆压缩都增加了“严格名字锁定”说明，避免把相似名字误当成本体。
4. 自动写日记的任务 prompt 已改成以“你”来下达指令，避免把“我”误解成用户视角。
5. 保留了旧逻辑的回退路径，开关关闭后仍可回到原先的轻量提示词模式。

---

## 2. 这次为什么会出现人称漂移

原实现里，自动写日记和连续记忆压缩都没有走主回复模型那套系统提示词组装流程，而是自己直接拼了一个很短的 `LLMRequest`：

1. 只有日记插件自己的任务说明。
2. 没有主回复模型的人设、表达风格、场景引导和安全约束。
3. 自动写日记的 user prompt 里还直接写了“用 `我` 的口吻”，容易让模型把“我”理解成当前对话里的用户，而不是日记主体。

这会导致模型在以下情况下更容易漂：

1. 对话里出现多个接近“爱莉希雅”的名字。
2. 任务 prompt 和主回复 prompt 的指代关系不一致。
3. 连续压缩时只有摘要任务，没有主回复模型的人设上下文，模型更容易退化成中性摘要口吻。

---

## 3. 实现内容

### 3.1 共享完整人设提示词

新增了一个共享构建器，用来从 `default_chatter` 的 prompt 模板里取出完整系统人设：

1. 优先读取 `default_chatter_system_prompt` 模板。
2. 自动填充 `platform / chat_type / nickname / bot_id / theme_guide`。
3. 如果模板不可用，则使用回退版构建，避免直接失败。

这样自动写日记和连续记忆压缩看到的，不再只是短任务提示，而是与主回复模型一致的人设底座。

### 3.2 自动写日记 prompt 调整

自动写日记 prompt 现在改成：

1. 先加载完整主回复人设。
2. 再叠加自动日记任务说明。
3. 明确要求日记主体稳定指向本体，不要写成用户视角。
4. 不再在任务指令里直接强调“用 `我` 的口吻”。

同时增加了严格名字锁定：

1. 只有完全匹配核心昵称才算本体。
2. 英文、缩写、少字、多字、大小写变化、带符号写法都不算同一个人。
3. 相似名字一律按其他用户处理。

### 3.3 连续记忆压缩 prompt 调整

连续记忆压缩也走了同一套共享人设提示词，并补上同样的名字锁定规则。

这样可以让压缩模型保持和主回复模型一致的主体视角，减少记忆摘要里出现“视角漂移”。

### 3.4 新增开关

新增了两个可视化配置开关：

1. `inherit_default_chatter_persona_prompt`
2. `strict_identity_name_lock`

默认都开启，便于直接获得稳定效果；如果后续需要排查问题，可以单独关闭。

---

## 4. 修改文件

### 已修改

- `plugins/diary_plugin/config.py`
- `plugins/diary_plugin/prompts.py`
- `plugins/diary_plugin/event_handler.py`
- `plugins/diary_plugin/service.py`

---

## 5. 验证结果

已完成以下验证：

```bash
python -m py_compile plugins/diary_plugin/prompts.py plugins/diary_plugin/event_handler.py plugins/diary_plugin/service.py plugins/diary_plugin/config.py
pytest -q -o addopts='' test/plugins/diary_plugin/test_service.py
pytest -q -o addopts='' test/plugins/diary_plugin/test_event_handler.py
```

结果：

1. 语法检查通过。
2. `diary_plugin` 相关单测通过。
3. 自动写日记与连续记忆压缩链路没有被改坏。

---

## 6. 影响评估

### 原有功能

原有的按天写日记、读日记、自动触发写日记、连续记忆同步和压缩流程都保留了。

### 新增影响

1. 自动写日记和连续记忆压缩的 prompt 更完整。
2. 相似名字更不容易被误并到本体。
3. 更容易避免“日记从爱莉的视角漂成用户视角”的问题。

### 回退路径

如果后续发现共享人设提示词过强或过长，可以通过配置关闭：

1. 关闭 `inherit_default_chatter_persona_prompt`
2. 关闭 `strict_identity_name_lock`

---

## 7. 备注

本次修改没有触碰 `default_chatter` 主回复链路本身，只是让 `diary_plugin` 的内部 LLM 任务复用它的完整提示词结构。

如果你后面要继续做可视化 prompt debug，下一步最适合接的是把这套共享构建器再接到日志输出里，这样就能直接看到“某个任务最终喂给模型的完整可读 prompt”。

