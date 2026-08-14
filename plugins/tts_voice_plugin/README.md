# TTS Voice Plugin (tts_voice_plugin)

> **遗留兼容组件：** 当前爱莉的本地 TTS 主路径是 `tasks.tts → IndexTTS2`，不经过本插件。本 README 只说明仍保留的 GPT-SoVITS/Higgs 协议实现；不得据此把 GPT-SoVITS 写成项目当前 TTS 模型。

文本转语音插件，为 Elysium 提供高质量、多语言、多风格的语音合成能力。当前本地音色基线为 IndexTTS2，并可接入已配置的云端语音服务。

## 🌟 功能特性

- **多语言支持**：支持中文 (zh)、英文 (en)、日文 (ja)、粤语 (yue) 等，具备智能语言检测功能。
- **多风格切换**：支持配置多个语音风格（模型权重+参考音频），并可根据需求动态切换。
- **智能文本清洗**：自动处理文本中的特殊符号、表情符号缩写（如 `www`, `233`），并进行智能截断以适应 TTS 合成。
- **空间音效处理**：内置基于 `Pedalboard` 的音效处理器，支持标准混响 (Reverb) 和卷积混响 (Convolution)，营造更真实的听感。
- **灵活的触发方式**：
  - **Action 模式**：允许 AI 根据上下文主动决定是否发送语音。
  - **Command 模式**：支持通过指令手动触发语音合成。
- **模型动态切换**：本地 GPT-SoVITS 支持在合成前动态切换 GPT 和 SoVITS 模型权重。
- **云端音色克隆**：支持 Boson Higgs Audio custom voice；未配置 voice ID 时可回退到参考音频一次性克隆。

## 🛠️ 安装依赖

本插件需要 `aiohttp`、`soundfile` 和 `pedalboard`。启用前必须由开发者将它们加入 `pyproject.toml` 并更新 `uv.lock`，随后由部署入口安装锁定依赖：

```bash
./deploy.sh bootstrap
```

禁止在生产启动时临时 `pip install` 或 `uv pip install`。未入锁的依赖表示该可选插件尚未具备可复现部署条件，应保持关闭。

> **注意**：使用 `gpt_sovits` 引擎时需要一个运行中的 [GPT-SoVITS API 服务](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/docs/cn/API.md)。使用 `higgs_cloud` 引擎时需要配置 Boson API Key。

## ⚙️ 配置说明

配置文件位于 `config/plugins/tts_voice_plugin/config.toml`。

### 基础配置 `[plugin]`
- `enable`: 是否启用插件。
- `keywords`: 触发语音合成的关键词列表。

### TTS 服务配置 `[tts]`
- `engine`: TTS 引擎。可选 `gpt_sovits`、`mimo_cloud`、`higgs_cloud`。
- `server`: GPT-SoVITS API 服务地址（默认 `http://127.0.0.1:9880`）。
- `max_text_length`: 单次合成的最大文本长度。

### 语音风格配置 `[[tts_styles]]`
你可以配置多个风格，必须包含一个名为 `default` 的风格。
- `style_name`: 风格唯一标识。
- `name`: 显示名称。
- `refer_wav_path`: 参考音频的绝对路径。
- `prompt_text`: 参考音频对应的文本内容。
- `gpt_weights`: GPT 模型权重路径。
- `sovits_weights`: SoVITS 模型权重路径。
- `text_language`: 文本语言模式 (`zh`/`en`/`ja`/`yue`/`auto`/`auto_yue`)。

### Boson Higgs 配置 `[higgs_cloud]`
- `base_url`: Boson Create Speech 接口地址，默认 `https://api.boson.ai/v1/audio/speech`。
- `api_key`: Boson API Key。
- `model`: Higgs TTS 模型，默认 `higgs-audio-v3-tts`。
- `voice`: 已注册的 custom voice ID。为空时，插件会使用当前风格的 `refer_wav_path` 和 `prompt_text` 做一次性参考音频克隆。
- `response_format`: 输出音频格式，默认 `mp3`。

### 空间音效 `[spatial_effects]`
- `enabled`: 是否启用音效。
- `reverb_enabled`: 是否启用标准混响。
- `convolution_enabled`: 是否启用卷积混响（需在插件 `assets/` 目录下放置 `small_room_ir.wav`）。

## 🚀 使用方法

1. **主动语音**：在对话中，如果 AI 认为需要使用语音表达（或匹配到关键词），它会自动调用 `tts_voice_action`。
2. **手动指令**：使用配置的指令（如 `/tts`，具体取决于指令组件实现）来合成特定文本。

## 📄 开源协议

本项目采用 [AGPL-v3.0](LICENSE) 协议。
