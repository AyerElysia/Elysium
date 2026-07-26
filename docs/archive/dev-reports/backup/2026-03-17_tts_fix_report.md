# TTS 插件修复与命令路由兼容报告（2026-03-17）

## 处理内容
1. **命令路由兼容 @ 提及**
   - `src/core/managers/command_manager.py` 现会剥离开头的 `@<nick:id>` 片段，再做命令判定，`@爱莉 /tts ...` 可被命中。
2. **TTS 插件导入与组件补全**
   - `tts_voice_plugin` 目录补齐 manifest、入口，组件包含 `TTSVoiceService` / `TTSVoiceCommand` / `TTSVoiceAction`。
   - 新增 `send_tts_voice` 动作，供 default_chatter 主动调用语音。
3. **日志接口不兼容 bug 修复**
   - `plugins/tts_voice_plugin/service.py` 中 logger 调用改为单字符串 f-string，避免 `Logger.info() takes 2 positional arguments` 异常。
4. **Napcat 语音发送兼容修复（最新）**
   - 修复了 Napcat 报 `语音转换失败, 请检查语音文件是否正常` 的错误。
   - 将发送给 Napcat 的数据从 `data:audio/ogg;base64,...` 改为纯净的 `base64://...`，以匹配 `napcat_adapter` 预期的语音格式。

## 需要的运行步骤
1. 启动 GPT-SoVITS API（不要用 webui.py）：
   ```bash
   cd /root/Elysia/GPT-SoVITS
   python api_v2.py -a 0.0.0.0 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
   ```
2. 启动 Neo-MoFox（代码有改动，需重启生效）：
   ```bash
   cd /root/Elysia/Neo-MoFox_Deployment/Neo-MoFox
   uv run main.py
   ```
3. 验证命令与动作：
   - 命令：`/tts 你好呀，今天天气真不错 default`（或 `@爱莉 /tts ...`）。
   - 主动动作：在私聊/明确请求语音场景，LLM 会调用 `action-send_tts_voice`。

## 观测点
- 若依然出现 Napcat 错误，可能是 FFmpeg 环境问题导致 Napcat 无法将 `.ogg` 转换成 QQ 的 Silk 格式。如果发生，请直接回复。

## 00:37 后追加
- Napcat 报错 `retcode 1200: 语音转换失败` 的原因：它期望 `base64://`，但收到 `data:...;base64,...`。
- 已修改 `tts_voice_plugin/service.py`：返回的音频改为纯 `base64://{b64}`，无需 data URL 头。
- 生效方式：重启 Neo-MoFox 以加载新的插件代码，重测 `/tts` 或主动语音。
