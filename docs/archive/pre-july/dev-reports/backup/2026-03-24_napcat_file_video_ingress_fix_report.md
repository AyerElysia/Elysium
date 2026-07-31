# Napcat 文件视频识别修复报告（2026-03-24）

## 问题现象
- 用户发送 `0001-0510.mp4` 后，日志显示：
  - `napcat_adapter | 收到文件消息 ...`
  - `消息接收器 | [文件:0001-0510.mp4]`
- 结果：消息被当作 `file`，没有进入 `video` 处理与视频摘要链路。

## 根因分析
- `napcat_adapter` 的 `file` 分支此前无条件返回 `{"type":"file"}`。
- `message_converter` 只有在收到 `type=video` 时才会调用 `MediaManager.recognize_video()`。
- 因此即使文件后缀是 `.mp4`，也不会触发摘要。

## 修复内容
文件：`plugins/napcat_adapter/src/handlers/to_core/message_handler.py`

1. 新增视频文件扩展名识别（`.mp4/.mov/.webm/...`）。
2. `RealMessageType.file` 分支改为传入 `raw_message`，用于必要时补拉消息详情。
3. 在 `_handle_file_message` 中：
   - 若识别为视频文件且 `enable_video_processing=true`：
     - 优先从 file 段提取 `url/path`；
     - 如缺失则补拉 `get_msg` 再提取；
     - 成功则转走 `_handle_video_message`，输出 `type=video`。
   - 若无可用视频源，或视频处理开关关闭，则保持原 `file` 行为。
4. 增加关键日志，便于定位：
   - `检测到视频文件扩展名，按视频链路处理`
   - `视频文件消息补拉详情成功...`
   - `检测到视频文件扩展名，但缺少可用 URL/路径，回退为普通文件消息`

## 第二轮增强（针对 file_id-only 场景）
- 线上日志显示 `file` 段仅有 `file_id`，没有 `url/path`，第一轮修复仍会回退为普通文件。
- 已新增第二层来源补全：
  - 依次尝试 `get_file(file_id)` / `get_file(file_name)` / `get_private_file_url(file_id)`（群聊再试 `get_group_file_url`）。
  - 只要拿到 `base64`、`url`、`path` 任一来源，就转为 `video` 段。
- 新增 `base64` 直通处理：
  - `_handle_video_message` 支持直接消费 `base64`，避免二次下载。

新增日志点：
- `get_file 成功获取视频 base64 数据`
- `get_private_file_url 成功获取视频 URL`
- `检测到视频文件扩展名，已通过补全拿到 base64，按视频链路处理`

## 兼容性说明
- 非视频文件逻辑不变。
- 关闭视频处理开关时，仍按原 file 逻辑处理，不破坏原配置语义。

## 验证
- 已执行：
  - `python -m py_compile plugins/napcat_adapter/src/handlers/to_core/message_handler.py`
- 运行时建议观察：
  - `napcat_adapter` 是否打印“按视频链路处理”
  - `message_converter` 是否不再出现 `[文件:xxx.mp4]`，而进入视频占位/摘要替换链路
