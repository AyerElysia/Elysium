# 2026-03-24 DFC 非原生视频理解打通报告

## 目标

在不启用“原生视频输入”的前提下，让 DFC 能理解用户发来的视频内容，并在主对话中可见。

要求：

- 不改 DFC 原生多模态协议（仍以图片原生多模态为主）
- 先打通可落地链路：视频 -> 关键帧 -> 文本理解 -> 注入主对话
- 保持已有图片/表情包逻辑兼容

---

## 最终方案

采用“非原生视频摘要链路”：

1. 接收端识别 `video` 消息段并落入媒体列表
2. `MessageConverter` 调用 `MediaManager.recognize_video(...)`
3. `MediaManager` 执行：
   - 提取视频 base64
   - 使用 `ffmpeg` 抽取最多 3 帧关键帧
   - 每帧复用现有 `recognize_media(..., "image")` 能力做图像识别
   - 用 `video` 任务模型（若存在）把关键帧描述汇总为简短视频摘要
4. 回写到 `processed_plain_text` 占位符：
   - `[视频]` -> `[视频:xxx摘要]`
5. DFC 像处理普通文本一样读取该摘要并参与回复决策

---

## 关键改动

## 1) `src/core/transport/message_receive/converter.py`

- 新增 `video` 段处理分支：
  - `case "video"` + `_handle_video(...)`
  - 统一占位符为 `[视频]`
- 扩展消息类型推断：
  - `video -> MessageType.VIDEO`
- 扩展媒体识别流程：
  - `_recognize_media_with_manager(..., skip_image_emoji=False)`
  - 支持视频识别与占位替换 `[视频:...]`
- 调整 skip VLM 逻辑：
  - 当某流开启 `skip_vlm` 时，仅跳过 `image/emoji` 识别
  - `video` 仍走非原生摘要链路（避免 DFC 原生模式下视频完全不可见）

## 2) `src/core/managers/media_manager.py`

- 初始化时加载 `video` 任务模型（可选）
- 新增 `recognize_video(...)`：
  - 缓存命中 -> 直接返回
  - 抽帧 -> 关键帧图片识别 -> 汇总摘要 -> 写缓存
- 新增 `_extract_video_keyframes(...)`：
  - 依赖 `ffmpeg` 抽取关键帧
- 新增 `_summarize_video_frames(...)`：
  - 通过模型把多帧描述压成 60~120 字摘要
- 新增 `_extract_video_payload(...)`：
  - 兼容字符串/字典两种视频数据载体

## 3) `src/app/plugin_system/api/media_api.py`

- 扩展 `media_type` 校验，支持 `video`
- `recognize_media(..., media_type="video")` 路由到 `MediaManager.recognize_video(...)`
- `recognize_batch(...)` 兼容 `video`，逐条走统一路由

---

## 验证结果

## 编译检查

执行：

```bash
python -m py_compile \
  /root/Elysia/Neo-MoFox/src/core/managers/media_manager.py \
  /root/Elysia/Neo-MoFox/src/core/transport/message_receive/converter.py \
  /root/Elysia/Neo-MoFox/src/app/plugin_system/api/media_api.py
```

结果：通过。

## 自动化测试

新增：

- `test/core/transport/test_message_converter_video.py`

说明：

- 当前环境缺少 `pytest-asyncio`，异步 pytest 被跳过（不是失败）

## 运行时冒烟验证（已执行）

通过 `python + asyncio.run(...)` 进行两组验证：

1. 视频段输入后，`processed_plain_text` 成功出现 `[视频:测试视频摘要]`
2. 开启 `skip_vlm` 时，图片保持 `[图片]`，但视频仍能变为 `[视频:视频摘要-OK]`

结论：核心链路已打通，行为符合预期。

---

## 你会看到的效果

用户发视频后，主模型上下文不再只有“[视频]”占位，而会变成可理解文本，例如：

`[视频:画面中有人在室外慢跑，背景是街道和建筑。]`

这段文本会进入 DFC 正常推理流程，从而实现“非原生视频可理解”。

---

## 边界与说明

- 这版不是原生视频 token 输入；本质是“关键帧视觉+文本总结”
- 摘要质量受关键帧抽样与 VLM 质量影响
- 需要系统有 `ffmpeg`；缺失时会降级为无法抽帧（不影响主流程稳定性）

---

## 后续可选优化（未做）

- 按视频时长做均匀采样而不是前 N 帧
- 对长视频加入“开头/中段/结尾”三段摘要
- 缓存中记录时长、帧率、关键帧时间戳，增强时间线表达

