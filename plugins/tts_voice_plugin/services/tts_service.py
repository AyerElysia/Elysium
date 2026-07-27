"""TTS 核心服务。

封装 GPT-SoVITS 语音合成的核心逻辑，包括风格管理、文本清洗、API 调用和空间音效处理。
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
from typing import TYPE_CHECKING, Any

import aiohttp
import soundfile as sf
from pedalboard import Convolution, Pedalboard, Reverb
from pedalboard.io import AudioFile

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.service import BaseService

if TYPE_CHECKING:
    from src.core.components.base.plugin import BasePlugin

    from ..config import TTSVoiceConfig

logger = get_logger("tts_voice_plugin.service")


HIGGS_CONTROL_TAG_RE = re.compile(r"<\|(emotion|style|sfx|prosody):[a-z_]+?\|>")


class TTSService(BaseService):
    """GPT-SoVITS TTS 核心服务。"""

    service_name: str = "tts"
    service_description: str = "GPT-SoVITS 语音合成服务"
    version: str = "3.1.2"

    def __init__(self, plugin: "BasePlugin") -> None:
        """初始化 TTS 服务。

        Args:
            plugin: 所属插件实例
        """
        super().__init__(plugin)
        self.tts_styles: dict[str, dict[str, Any]] = {}
        self.timeout: int = 60
        self.max_text_length: int = 500
        self._load_config()

    # ------------------------------------------------------------------
    # 配置加载
    # ------------------------------------------------------------------

    @property
    def _config(self) -> "TTSVoiceConfig":
        """获取当前插件配置的快捷属性。"""
        return self.plugin.config  # type: ignore[return-value]

    def _load_config(self) -> None:
        """从插件配置加载 TTS 参数。"""
        try:
            cfg = self._config
            self.timeout = cfg.tts.timeout
            self.max_text_length = cfg.tts.max_text_length
            self.tts_styles = self._load_tts_styles()

            if self.tts_styles:
                logger.info(f"TTS服务已成功加载风格: {list(self.tts_styles.keys())}")
            else:
                logger.warning("TTS风格配置为空，请检查配置文件")
        except Exception as e:
            logger.error(f"TTS服务配置加载失败: {e}")

    def _load_tts_styles(self) -> dict[str, dict[str, Any]]:
        """加载 TTS 风格配置。"""
        styles: dict[str, dict[str, Any]] = {}
        cfg = self._config
        global_server = cfg.tts.server
        tts_styles_list = cfg.tts_styles

        if not tts_styles_list:
            logger.error("tts_styles 配置为空列表")
            return styles

        default_cfg = next((s for s in tts_styles_list if s.style_name == "default"), None)
        if not default_cfg:
            logger.error("在 tts_styles 配置中未找到 'default' 风格，这是必需的。")
            return styles

        default_refer_wav = default_cfg.refer_wav_path
        default_prompt_text = default_cfg.prompt_text
        default_gpt_weights = default_cfg.gpt_weights
        default_sovits_weights = default_cfg.sovits_weights

        if not default_refer_wav:
            logger.warning("TTS 'default' style is missing 'refer_wav_path'.")

        for style_cfg in tts_styles_list:
            style_name = style_cfg.style_name
            if not style_name:
                continue

            styles[style_name] = {
                "style_name": style_name,
                "url": global_server,
                "name": style_cfg.name or style_name,
                "refer_wav_path": style_cfg.refer_wav_path or default_refer_wav,
                "prompt_text": style_cfg.prompt_text or default_prompt_text,
                "prompt_language": style_cfg.prompt_language or "zh",
                "gpt_weights": style_cfg.gpt_weights or default_gpt_weights,
                "sovits_weights": style_cfg.sovits_weights or default_sovits_weights,
                "speed_factor": style_cfg.speed_factor,
                "text_language": style_cfg.text_language or "auto",
            }
        return styles

    def get_available_styles(self) -> list[str]:
        """获取可用语音风格名称列表。

        Returns:
            可用风格名称列表
        """
        return list(self.tts_styles.keys())

    # ------------------------------------------------------------------
    # 语言检测与规范化
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_language_code(language_str: str | None) -> str:
        """提取括号前语言代码并统一为小写。"""
        if not language_str:
            return ""
        return language_str.split("(")[0].strip().lower()

    def _get_supported_language_codes(self) -> set[str]:
        """从配置加载支持的 text_lang 代码集合。"""
        raw_codes = self._config.tts.supported_text_languages or []
        supported_codes = {
            code
            for code in (self._extract_language_code(item) for item in raw_codes)
            if code
        }
        if supported_codes:
            return supported_codes

        fallback_code = self._extract_language_code(self._config.tts.fallback_text_language)
        if fallback_code:
            logger.warning(
                "配置项 tts.supported_text_languages 为空，将临时使用 fallback_text_language 作为支持语言。"
            )
            return {fallback_code}

        logger.warning(
            "配置项 tts.supported_text_languages 与 fallback_text_language 均为空，使用内置兜底语言: zh"
        )
        return {"zh"}

    def _get_fallback_language_code(self, supported_codes: set[str] | None = None) -> str:
        """获取有效的兜底语言代码。"""
        supported = supported_codes or self._get_supported_language_codes()
        configured_fallback = self._extract_language_code(self._config.tts.fallback_text_language)
        if configured_fallback and configured_fallback in supported:
            return configured_fallback

        if configured_fallback and configured_fallback not in supported:
            logger.warning(
                f"配置的 fallback_text_language='{configured_fallback}' 不在 supported_text_languages 中，"
                "将回退到可用语言。"
            )

        if "zh" in supported:
            return "zh"

        if supported:
            fallback = sorted(supported)[0]
            logger.warning(f"无法使用中文兜底，改为使用首个可用语言: {fallback}")
            return fallback

        return "zh"

    def _normalize_language_code(self, language_str: str) -> str:
        """规范化语言代码，并确保在配置允许的集合内。"""
        supported_codes = self._get_supported_language_codes()
        fallback_code = self._get_fallback_language_code(supported_codes)
        base_code = self._extract_language_code(language_str)
        if not base_code:
            return fallback_code
        if base_code in supported_codes:
            return base_code

        logger.warning(
            f"无效或不受支持的语言代码 '{language_str}'，已规范化为兜底语言: {fallback_code}"
        )
        return fallback_code

    def _sanitize_language_hint(self, language_hint: str | None) -> str | None:
        """清洗决策模型给出的语言提示。"""
        raw_code = self._extract_language_code(language_hint)
        if not raw_code:
            return None
        normalized_code = self._normalize_language_code(raw_code)
        if normalized_code != raw_code:
            logger.warning(
                f"决策模型指定语言 '{language_hint}' 不受支持，已回退为: {normalized_code}"
            )
        return normalized_code

    def _analyze_text_language(self, text: str) -> tuple[str, str]:
        """分析文本内容，自动检测语言类型。

        返回主要语言和是否为混合语言的标识。

        Args:
            text: 要分析的文本

        Returns:
            (主要语言代码, 混合类型描述)
            例如: ("zh", "已检测中文")、("en", "已检测英文")、("zh", "中英混合")
        """
        # 字符统计
        zh_count = len(re.findall(r"[\u4e00-\u9fff]", text))
        en_count = len(re.findall(r"[a-zA-Z]", text))
        ja_count = len(re.findall(r"[\u3040-\u309f\u30a0-\u30ff]", text))

        total_chars = zh_count + en_count + ja_count

        if total_chars == 0:
            return "zh", "缺省（未检测中英文）"

        # 计算比例
        zh_ratio = zh_count / total_chars
        en_ratio = en_count / total_chars
        ja_ratio = ja_count / total_chars

        # 粤语检测
        cantonese_keywords = ["嘅", "喺", "咗", "唔", "係", "啲", "咩", "乜", "喂"]
        has_cantonese = any(keyword in text for keyword in cantonese_keywords)

        # 返回主要语言和混合信息
        if ja_ratio > 0.3:
            return "ja", f"已检测日语(占比{ja_ratio*100:.0f}%)"
        elif has_cantonese:
            return "yue", "已检测粤语关键词"
        elif en_ratio > 0.3:
            if zh_ratio > 0.1:
                return "en", f"中英混合(中{zh_ratio*100:.0f}%,英{en_ratio*100:.0f}%)"
            else:
                return "en", f"已检测英文(占比{en_ratio*100:.0f}%)"
        else:
            if en_ratio > 0.05:
                return "zh", f"中英混合(中{zh_ratio*100:.0f}%,英{en_ratio*100:.0f}%)"
            else:
                return "zh", "已检测纯中文"

    def _determine_final_language(self, text: str, mode: str) -> str:
        """根据配置的语言策略和文本内容，决定最终发送给 API 的语言代码。

        使用规范化的语言代码，智能检测文本语言特征。

        参数说明:
        - mode: 语言配置模式
          * 标准语言代码 (zh/en/ja/yue): 直接使用
          * 带描述格式 (zh(中英混合)): 自动提取代码
          * auto: 根据文本自动检测
          * auto_yue: 自动检测，优先检查粤语

        Args:
            text: 要合成的文本
            mode: 语言模式配置

        Returns:
            最终语言代码字符串
        """
        # 第一步：规范化配置中的语言代码
        normalized_mode = self._normalize_language_code(mode)

        # 第二步：如果已是确定的语言代码（不是auto模式），直接返回
        if normalized_mode not in ["auto", "auto_yue"]:
            logger.info(f"使用配置的语言代码: {normalized_mode}")
            return normalized_mode

        # 第三步：自动分析文本语言
        detected_lang, detection_info = self._analyze_text_language(text)

        # 特殊处理 auto_yue 模式
        if normalized_mode == "auto_yue":
            final_lang = self._normalize_language_code(detected_lang)
            logger.info(f"auto_yue 模式 - {detection_info}，最终语言: {final_lang}")
            return final_lang

        # 通用 auto 模式
        if detected_lang == "zh" and "中英混合" in detection_info:
            # 中英混合时优先用中文（大多数API支持更好）
            final_lang = self._normalize_language_code("zh")
            logger.info(f"auto 模式 - {detection_info}，以中文处理，最终语言: {final_lang}")
            return final_lang
        else:
            final_lang = self._normalize_language_code(detected_lang)
            logger.info(f"auto 模式 - {detection_info}，最终语言: {final_lang}")
            return final_lang

    # ------------------------------------------------------------------
    # 文本清洗
    # ------------------------------------------------------------------

    def _clean_text_for_tts(self, text: str) -> str:
        """清洗文本以适合 TTS 合成。

        Args:
            text: 原始文本

        Returns:
            清洗后的文本
        """
        protected_tags: dict[str, str] = {}

        def protect_higgs_tag(match: re.Match[str]) -> str:
            placeholder = f"HIGGSTAG{len(protected_tags)}"
            protected_tags[placeholder] = match.group(0)
            return placeholder

        if self._config.tts.engine == "higgs_cloud":
            text = HIGGS_CONTROL_TAG_RE.sub(protect_higgs_tag, text)

        # 1. 基本清理
        text = re.sub(r"[\(（\[【].*?[\)）\]】]", "", text)
        text = re.sub(r"([，。！？、；：,.!?;:~\-`])\1+", r"\1", text)
        text = re.sub(r"~{2,}|～{2,}", "，", text)
        text = re.sub(r"\.{3,}|…{1,}", "。", text)

        # 2. 词语替换
        replacements = {"www": "哈哈哈", "hhh": "哈哈", "233": "哈哈", "666": "厉害", "88": "拜拜"}
        for old, new in replacements.items():
            text = text.replace(old, new)

        # 3. 移除不必要的字符
        text = re.sub(
            r"[^\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ffa-zA-Z0-9\s，。！？、；：,.!?;:~～]",
            "",
            text,
        )

        # 4. 确保结尾有标点
        if text and not text.endswith(tuple("，。！？、；：,.!?;:")):
            text += "。"

        # 5. 智能截断
        if len(text) > self.max_text_length:
            cut_text = text[: self.max_text_length]
            punctuation = "。！？.…"
            last_punc_pos = max(cut_text.rfind(p) for p in punctuation)

            if last_punc_pos != -1:
                text = cut_text[: last_punc_pos + 1]
            else:
                last_comma_pos = max(cut_text.rfind(p) for p in "，、；,;")
                if last_comma_pos != -1:
                    text = cut_text[: last_comma_pos + 1]
                else:
                    text = cut_text

        for placeholder, tag in protected_tags.items():
            text = text.replace(placeholder, tag)

        return text.strip()

    # ------------------------------------------------------------------
    # TTS API 调用
    # ------------------------------------------------------------------

    async def _call_tts_api(
        self,
        server_config: dict[str, Any],
        text: str,
        text_language: str,
        **kwargs: Any,
    ) -> bytes | None:
        """调用 GPT-SoVITS API 进行语音合成。

        先切换模型权重，再发送合成请求。

        Args:
            server_config: 风格服务配置
            text: 合成文本
            text_language: 文本语言
            **kwargs: 额外参数 (refer_wav_path, prompt_text, gpt_weights 等)

        Returns:
            音频字节数据，失败返回 None
        """
        ref_wav_path = kwargs.get("refer_wav_path")
        if not ref_wav_path:
            logger.error(f"API 调用失败：缺少 refer_wav_path。当前风格配置: {server_config}")
            return None

        try:
            base_url = server_config["url"].rstrip("/")

            # 步骤一：切换模型权重
            async def switch_model_weights(weights_path: str | None, weight_type: str) -> None:
                if not weights_path:
                    return
                api_endpoint = f"/set_{weight_type}_weights"
                switch_url = f"{base_url}{api_endpoint}"
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=self.timeout)
                    ) as session:
                        async with session.get(switch_url, params={"weights_path": weights_path}) as resp:
                            if resp.status != 200:
                                error_text = await resp.text()
                                logger.error(f"切换 {weight_type} 模型失败: {resp.status} - {error_text}")
                            else:
                                logger.info(f"成功切换 {weight_type} 模型为: {weights_path}")
                except Exception as e:
                    logger.error(f"请求切换 {weight_type} 模型时发生网络异常: {e}")

            await switch_model_weights(kwargs.get("gpt_weights"), "gpt")
            await switch_model_weights(kwargs.get("sovits_weights"), "sovits")

            # 步骤二：构建合成请求数据
            data: dict[str, Any] = {
                "text": text,
                "text_lang": text_language,
                "ref_audio_path": ref_wav_path,
                "prompt_text": kwargs.get("prompt_text", ""),
                "prompt_lang": kwargs.get("prompt_language", "zh"),
            }

            # 合并高级配置
            cfg = self._config
            advanced_dict = cfg.tts_advanced.model_dump()
            data.update({k: v for k, v in advanced_dict.items() if v is not None})

            # 优先使用风格特定的语速
            if server_config.get("speed_factor") is not None:
                data["speed_factor"] = server_config["speed_factor"]

            # 步骤三：发送合成请求
            tts_url = base_url if base_url.endswith("/tts") else f"{base_url}/tts"
            logger.info(f"发送到 TTS API 的数据: {data}")

            connector = aiohttp.TCPConnector(limit=100)
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.post(tts_url, json=data) as response:
                    if response.status == 200:
                        audio_data = bytearray()
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            audio_data.extend(chunk)
                        logger.info(f"成功接收音频数据，大小: {len(audio_data)} 字节")
                        return bytes(audio_data)
                    else:
                        error_info = await response.text()
                        logger.error(f"TTS API调用失败: {response.status} - {error_info}")
                        return None

        except asyncio.TimeoutError:
            logger.error("TTS服务请求超时")
            return None
        except Exception as e:
            logger.error(f"TTS API调用异常: {e}")
            return None

    async def _call_mimo_cloud_tts(
        self,
        server_config: dict[str, Any],
        text: str,
    ) -> bytes | None:
        """调用 MiMo 云端 VoiceClone API。"""
        mimo_cfg = self._config.mimo_cloud
        base_url = mimo_cfg.base_url
        api_key = mimo_cfg.api_key
        model = mimo_cfg.model
        
        if not api_key:
            logger.error("MiMoCN API Key 未配置！")
            return None

        ref_wav_path = server_config.get("refer_wav_path")
        prompt_text = server_config.get("prompt_text", "")
        
        if not ref_wav_path or not os.path.exists(ref_wav_path):
            logger.error(f"云端克隆失败：参考音频 {ref_wav_path} 不存在。")
            return None

        try:
            with open(ref_wav_path, "rb") as f:
                ref_audio_b64 = base64.b64encode(f.read()).decode("utf-8")
                voice_data_url = f"data:audio/wav;base64,{ref_audio_b64}"
        except Exception as e:
            logger.error(f"读取参考音频失败: {e}")
            return None

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model,
            "modalities": ["text", "audio"],
            "audio": { 
                "voice": voice_data_url,
                "prompt_text": prompt_text,
                "format": "wav" 
            },
            "messages": [
                { "role": "user", "content": "请用我的声音读出下面的话：" },
                { "role": "assistant", "content": text }
            ]
        }
        
        logger.info(f"发送 MiMo 云端克隆请求，使用模型: {model}")
        try:
            connector = aiohttp.TCPConnector(limit=10)
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.post(base_url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        message = data.get("choices", [{}])[0].get("message", {})
                        audio_info = message.get("audio")
                        if audio_info and "data" in audio_info:
                            logger.info("MiMo 云端克隆成功收到音频数据")
                            return base64.b64decode(audio_info["data"])
                        else:
                            logger.error(f"云端响应中没有音频数据: {data}")
                            return None
                    else:
                        error_text = await resp.text()
                        logger.error(f"MiMo 云端 API 调用失败: {resp.status} - {error_text}")
                        return None
        except asyncio.TimeoutError:
            logger.error("MiMo 云端 TTS 请求超时")
            return None
        except Exception as e:
            logger.error(f"MiMo 云端 TTS 异常: {e}")
            return None

    async def _call_higgs_cloud_tts(
        self,
        server_config: dict[str, Any],
        text: str,
    ) -> bytes | None:
        """调用 Boson Higgs Audio 云端 TTS。"""
        higgs_cfg = self._config.higgs_cloud
        base_url = higgs_cfg.base_url
        api_key = higgs_cfg.api_key

        if not api_key:
            logger.error("Boson API Key 未配置！")
            return None

        payload: dict[str, Any] = {
            "model": higgs_cfg.model,
            "input": text,
            "response_format": higgs_cfg.response_format,
            "num_timesteps": higgs_cfg.num_timesteps,
        }

        speed_factor = server_config.get("speed_factor")
        if speed_factor:
            payload["speed"] = speed_factor

        if higgs_cfg.voice:
            payload["voice"] = higgs_cfg.voice
        else:
            ref_wav_path = server_config.get("refer_wav_path")
            prompt_text = server_config.get("prompt_text", "")
            if not ref_wav_path or not os.path.exists(ref_wav_path):
                logger.error(f"Higgs 云端克隆失败：参考音频 {ref_wav_path} 不存在。")
                return None

            try:
                with open(ref_wav_path, "rb") as f:
                    ref_audio_bytes = f.read()
                    payload["ref_audio"] = base64.b64encode(ref_audio_bytes).decode("utf-8")
                payload["ref_text"] = prompt_text
                logger.info(
                    "Higgs 云端 TTS 使用参考原始音频: "
                    f"path={ref_wav_path}, size={len(ref_audio_bytes)} 字节"
                )
            except Exception as e:
                logger.error(f"读取 Higgs 参考音频失败: {e}")
                return None

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        logger.info(
            "发送 Higgs 云端 TTS 请求，"
            f"模型: {higgs_cfg.model}, voice: {higgs_cfg.voice or 'ref_audio'}, "
            f"format: {higgs_cfg.response_format}"
        )
        try:
            connector = aiohttp.TCPConnector(limit=10)
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.post(base_url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        audio_data = await resp.read()
                        logger.info(f"Higgs 云端 TTS 成功收到音频数据，大小: {len(audio_data)} 字节")
                        return audio_data

                    error_text = await resp.text()
                    logger.error(f"Higgs 云端 API 调用失败: {resp.status} - {error_text}")
                    return None
        except asyncio.TimeoutError:
            logger.error("Higgs 云端 TTS 请求超时")
            return None
        except Exception as e:
            logger.error(f"Higgs 云端 TTS 异常: {e}")
            return None

    # ------------------------------------------------------------------
    # 空间音效处理
    # ------------------------------------------------------------------

    async def _apply_spatial_audio_effect(self, audio_data: bytes) -> bytes | None:
        """根据配置应用空间效果（混响和卷积）。

        Args:
            audio_data: 原始音频字节

        Returns:
            处理后的音频字节，失败返回原始音频
        """
        try:
            effects_cfg = self._config.spatial_effects
            if not effects_cfg.enabled:
                return audio_data

            # 基于 __file__ 构建 IR 文件路径
            plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ir_path = os.path.join(plugin_dir, "assets", "small_room_ir.wav")

            effects: list[Any] = []

            if effects_cfg.reverb_enabled:
                effects.append(
                    Reverb(
                        room_size=effects_cfg.room_size,
                        damping=effects_cfg.damping,
                        wet_level=effects_cfg.wet_level,
                        dry_level=effects_cfg.dry_level,
                        width=effects_cfg.width,
                    )
                )

            if effects_cfg.convolution_enabled and os.path.exists(ir_path):
                effects.append(
                    Convolution(
                        impulse_response_filename=ir_path,
                        mix=effects_cfg.convolution_mix,
                    )
                )
            elif effects_cfg.convolution_enabled:
                logger.warning(f"卷积混响已启用，但IR文件不存在 ({ir_path})，跳过该效果。")

            if not effects:
                return audio_data

            with io.BytesIO(audio_data) as audio_stream:
                with AudioFile(audio_stream, "r") as f:
                    board = Pedalboard(effects)
                    effected = board(f.read(f.frames), f.samplerate)

            with io.BytesIO() as output_stream:
                sf.write(output_stream, effected.T, f.samplerate, format="WAV")
                processed_audio_data = output_stream.getvalue()

            logger.info("成功应用空间效果。")
            return processed_audio_data

        except Exception as e:
            logger.error(f"应用空间效果时出错: {e}")
            return audio_data

    # ------------------------------------------------------------------
    # 语音生成（主入口）
    # ------------------------------------------------------------------

    async def generate_voice(
        self,
        text: str,
        style_hint: str = "default",
        language_hint: str | None = None,
    ) -> str | None:
        """生成语音并返回 Base64 编码。

        Args:
            text: 要合成的文本
            style_hint: 风格名称提示
            language_hint: 语言提示 (优先级最高)

        Returns:
            Base64 编码的音频数据，失败返回 None
        """
        self._load_config()

        if not self.tts_styles:
            logger.error("TTS风格配置为空，无法生成语音。")
            return None

        # 风格选择回退逻辑
        style = style_hint if style_hint in self.tts_styles else "default"
        if style not in self.tts_styles:
            if "default" in self.tts_styles:
                style = "default"
                logger.warning(f"指定风格 '{style_hint}' 不存在，自动回退到: 'default'")
            elif self.tts_styles:
                style = next(iter(self.tts_styles))
                logger.warning(
                    f"指定风格 '{style_hint}' 和 'default' 均不存在，自动回退到第一个可用风格: {style}"
                )
            else:
                logger.error("没有任何可用的TTS风格配置")
                return None

        server_config = self.tts_styles[style]
        clean_text = self._clean_text_for_tts(text)
        if not clean_text:
            return None

        # 语言决策：优先 language_hint → 风格配置策略 → 自动检测
        sanitized_hint = self._sanitize_language_hint(language_hint)
        if sanitized_hint:
            final_language = sanitized_hint
            logger.info(f"使用决策模型指定的语言: {final_language}")
        else:
            language_policy = server_config.get("text_language", "auto")
            final_language = self._determine_final_language(clean_text, language_policy)
            logger.info(f"决策模型未指定语言，使用策略 '{language_policy}' -> 最终语言: {final_language}")

        logger.info(
            f"开始TTS语音合成，文本：{clean_text[:50]}..., 风格：{style}, 最终语言: {final_language}"
        )

        if self._config.tts.engine == "mimo_cloud":
            logger.info("引擎配置为 mimo_cloud，使用云端 VoiceClone")
            audio_data = await self._call_mimo_cloud_tts(
                server_config=server_config,
                text=clean_text,
            )
        elif self._config.tts.engine == "higgs_cloud":
            logger.info("引擎配置为 higgs_cloud，使用 Boson Higgs Audio")
            audio_data = await self._call_higgs_cloud_tts(
                server_config=server_config,
                text=clean_text,
            )
        else:
            audio_data = await self._call_tts_api(
                server_config=server_config,
                text=clean_text,
                text_language=final_language,
                refer_wav_path=server_config.get("refer_wav_path"),
                prompt_text=server_config.get("prompt_text"),
                prompt_language=server_config.get("prompt_language"),
                gpt_weights=server_config.get("gpt_weights"),
                sovits_weights=server_config.get("sovits_weights"),
            )

        if audio_data:
            # 空间音效处理
            spatial_cfg = self._config.spatial_effects
            if spatial_cfg.enabled:
                logger.info("检测到已启用空间音频效果，开始处理...")
                processed_audio = await self._apply_spatial_audio_effect(audio_data)
                if processed_audio:
                    logger.info("空间音频效果应用成功！")
                    audio_data = processed_audio
                else:
                    logger.warning("空间音频效果应用失败，将使用原始音频。")

            return base64.b64encode(audio_data).decode("utf-8")
        return None
