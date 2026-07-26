# 2026-03-24 DFC 原生视频多模态打通报告（含自动降级）

## 目标

在已完成“非原生视频摘要链路”的基础上，进一步打通 DFC 的原生视频输入能力，并保证稳定性：

- 支持将视频直接作为多模态内容发送给主模型
- 若上游模型/网关不支持视频 content 类型，自动回退到文本摘要链路
- 不影响已有图片/表情包原生多模态能力

---

## 实现概览

本次实现为“双通道”：

1. **首选原生视频输入**
   - DefaultChatter 在 `native_multimodal` 开启时，可把视频打包进 LLM payload
2. **失败自动降级**
   - OpenAI 客户端检测到“video 不支持”异常后，自动移除原生视频块并重试
   - 由于接收端已具备视频摘要文本（非原生链路），降级后仍可保持语义可见

结果：在支持视频的模型上用原生；不支持时不中断会话，自动回到摘要文本路径。

---

## 关键改动

## 1) 新增 `Video` payload 类型

文件：

- `src/kernel/llm/payload/content.py`
- `src/kernel/llm/payload/__init__.py`
- `src/kernel/llm/__init__.py`

改动：

- 新增 `Video(File)` 类，输入规范与 `Image/Audio` 一致（路径、文件对象、base64、data URL）
- 导出到 kernel llm 公共接口，供上层直接使用 `Video(...)`

## 2) OpenAI 客户端支持视频 content 并自动降级

文件：

- `src/kernel/llm/model_client/openai_client.py`

改动：

- `_payloads_to_openai_messages` 新增 `Video -> {"type":"video_url","video_url":{"url":...}}`
- 新增 `_video_to_data_url(...)`
- 新增视频检测/移除辅助：
  - `_messages_contain_native_video(...)`
  - `_strip_native_video_from_messages(...)`
  - `_is_native_video_unsupported_error(...)`
- 在 `_create_non_stream` 与 `_create_stream` 中加入自动降级重试：
  - 首次请求失败且命中“视频不支持”特征时
  - 自动移除原生视频块后重试一次
  - 日志记录降级行为（WARNING）

## 3) DefaultChatter 原生多模态扩展到视频

文件：

- `plugins/default_chatter/multimodal.py`
- `plugins/default_chatter/runners.py`
- `plugins/default_chatter/config.py`
- `plugins/default_chatter/plugin.py`

改动：

- 媒体提取从 image/emoji 扩展到 image/emoji/video
- 构建 payload 时支持 `Video(...)`
- 新增配置项：
  - `max_videos_per_payload`（默认 1）
  - `native_video_multimodal`（默认 true）
- 历史媒体注入逻辑同时考虑图片与视频预算
- 消息行媒体后缀增加视频计数（`视频×N`）

## 4) 请求检视器可读性增强

文件：

- `src/kernel/llm/request_inspector.py`

改动：

- 增加 `video_url/input_video` 可视化显示，便于排查请求结构

---

## 验证结果

## 1. 编译检查

执行：

```bash
python -m py_compile \
  /root/Elysia/Neo-MoFox/src/kernel/llm/payload/content.py \
  /root/Elysia/Neo-MoFox/src/kernel/llm/payload/__init__.py \
  /root/Elysia/Neo-MoFox/src/kernel/llm/__init__.py \
  /root/Elysia/Neo-MoFox/src/kernel/llm/model_client/openai_client.py \
  /root/Elysia/Neo-MoFox/src/kernel/llm/request_inspector.py \
  /root/Elysia/Neo-MoFox/plugins/default_chatter/multimodal.py \
  /root/Elysia/Neo-MoFox/plugins/default_chatter/runners.py \
  /root/Elysia/Neo-MoFox/plugins/default_chatter/config.py \
  /root/Elysia/Neo-MoFox/plugins/default_chatter/plugin.py
```

结果：通过。

## 2. 测试与冒烟

- `test/kernel/llm/test_content.py -k TestVideo`：通过
- `test/kernel/llm/test_openai_client.py -k multimodal_content_with_video`：通过
- 手写异步冒烟脚本验证“视频不支持时自动降级重试”：通过

补充：

- 当前环境缺 `pytest-asyncio`，异步 pytest 用例会被跳过；因此针对降级重试使用了脚本级冒烟验证。

---

## 运行时预期日志

当上游不支持原生视频时，你会看到类似日志：

- `OpenAI客户端 | WARNING | 检测到上游不支持原生视频输入，自动降级为文本摘要模式重试。移除视频块数量: N`

这表示系统已自动回退，不会因为视频输入导致整轮回复失败。

---

## 最终行为

当前 DFC 对视频的处理顺序是：

1. 优先尝试原生视频输入（若开启 `native_multimodal` 且 `native_video_multimodal=true`）
2. 若上游拒绝视频 content，自动降级移除视频块并重试
3. 依靠已存在的视频文本摘要继续回复

因此实现了“能用原生就原生，不支持就稳定回退”的目标。

