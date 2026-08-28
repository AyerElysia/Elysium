"""本地消息 TTS 核心服务。

封装风格管理、文本清洗、HTTP 后端调用和空间音效处理。后端由部署配置在
GPT-SoVITS ``api_v2`` 与 IndexTTS2.5 + vLLM-Omni 之间显式选择，不得从 URL
猜测，也不得把未绑定的云端 TTS 冒充成本机意识声音。
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import mimetypes
import os
import re
import shlex
import signal
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
import numpy as np
import soundfile as sf
from pedalboard import Convolution, Pedalboard, Reverb
from pedalboard.io import AudioFile

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.service import BaseService
from src.core.utils.audio_transcode import transcode_audio_bytes
from src.kernel.concurrency import get_task_manager

if TYPE_CHECKING:
    from src.core.components.base.plugin import BasePlugin
    from src.kernel.concurrency import TaskInfo

    from ..config import TTSVoiceConfig

logger = get_logger("tts_voice_plugin.service")


@dataclass(frozen=True, slots=True)
class _SynthesisSegment:
    """One internal transport segment of a single outward expression."""

    text: str
    boundary: str
    units: int


_LegacyWeightIdentity = tuple[str, int, int]


_TTS_CLARITY_WARNING_UNITS_PER_SECOND = 4.8


_SEMANTIC_BOUNDARY_RE = re.compile(
    r"(?:\r?\n){1,}|"
    r"[。！？!?]+[”’」』】）》)\]\"']*|"
    r"(?<!\d)\.(?!\d)[”’」』】）》)\]\"']*|"
    r"[；;：:]|[，,、]"
)


class TTSService(BaseService):
    """本地 TTS 核心服务。"""

    service_name: str = "tts"
    service_description: str = (
        "本地消息语音合成服务（GPT-SoVITS api_v2 / IndexTTS2.5 vLLM-Omni）"
    )
    version: str = "4.0.0"

    def __init__(self, plugin: "BasePlugin") -> None:
        """初始化 TTS 服务。

        Args:
            plugin: 所属插件实例
        """
        super().__init__(plugin)
        self.tts_styles: dict[str, dict[str, Any]] = {}
        self.timeout: int = 60
        self.max_text_length: int = 500
        self._server_process: asyncio.subprocess.Process | None = None
        self._server_start_lock = asyncio.Lock()
        self._idle_shutdown_task: TaskInfo | None = None
        self._legacy_weight_cache_owner: asyncio.subprocess.Process | None = None
        self._legacy_active_weights: dict[str, _LegacyWeightIdentity] = {}
        self._legacy_weight_cache_source: str | None = None
        self._idle_generation = 0
        # A complete outward expression owns one lane so two user-visible voices
        # cannot interleave. vLLM-Omni may still batch bounded internal segments.
        self._synthesis_lock = asyncio.Lock()
        self._load_config()

    async def stop(self) -> None:
        """Stop only the TTS server process group started by this service.

        An already-running external server is never adopted into
        ``_server_process`` and is therefore left untouched.  Processes created
        by :meth:`_start_server` own a new session so their two-stage vLLM
        workers can be reclaimed as one process group.
        """
        await self._cancel_idle_shutdown_task()
        await self._stop_owned_server_process(reason="plugin_stop")

    async def _stop_owned_server_process(
        self,
        *,
        reason: str,
        expected_process: asyncio.subprocess.Process | None = None,
    ) -> bool:
        """Stop one process group only while its local ownership is still exact."""
        process = self._server_process
        if expected_process is not None and process is not expected_process:
            return False
        if process is None:
            return False
        stopped = False
        try:
            if process.returncode is not None:
                return False
            try:
                self._signal_server_process(process, signal.SIGTERM)
            except ProcessLookupError:
                stopped = True
                return False

            try:
                await asyncio.wait_for(process.wait(), timeout=20.0)
                stopped = True
                logger.info(f"TTS 服务子进程已停止: reason={reason}")
                return True
            except asyncio.CancelledError:
                try:
                    self._signal_server_process(process, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.shield(process.wait())
                stopped = True
                raise
            except TimeoutError:
                logger.warning(
                    "TTS 服务子进程未在 20 秒内退出，清理已拥有的进程组"
                )

            try:
                self._signal_server_process(process, signal.SIGKILL)
            except ProcessLookupError:
                stopped = True
                return False
            await process.wait()
            stopped = True
            return True
        finally:
            # Ownership remains visible until the exact process has exited.
            # This prevents a new request from spawning a duplicate while the
            # old process still owns the listening port or GPU workers.
            if self._server_process is process and (
                stopped or process.returncode is not None
            ):
                self._server_process = None
                self._clear_legacy_weight_cache()

    async def _cancel_idle_shutdown_task(self) -> None:
        """Cancel and collect the currently owned idle timer, if any."""
        self._idle_generation += 1
        task_info = self._idle_shutdown_task
        self._idle_shutdown_task = None
        if task_info is None or task_info.task is None:
            return
        task = task_info.task
        if task is asyncio.current_task() or task.done():
            return
        task_info.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _arm_idle_shutdown(self) -> None:
        """Arm one content-free idle timer for the current owned process."""
        previous = self._idle_shutdown_task
        if previous is not None and not previous.is_done():
            previous.cancel()
        self._idle_shutdown_task = None
        self._idle_generation += 1

        idle_seconds = float(self._config.tts.idle_shutdown_seconds)
        process = self._server_process
        if (
            idle_seconds <= 0.0
            or process is None
            or process.returncode is not None
        ):
            return

        generation = self._idle_generation
        deadline = asyncio.get_running_loop().time() + idle_seconds
        self._idle_shutdown_task = get_task_manager().create_task(
            self._idle_shutdown_after(
                process=process,
                generation=generation,
                deadline=deadline,
                idle_seconds=idle_seconds,
            ),
            name="tts_voice_idle_shutdown",
            daemon=True,
            metadata={"idle_seconds": idle_seconds},
        )

    async def _idle_shutdown_after(
        self,
        *,
        process: asyncio.subprocess.Process,
        generation: int,
        deadline: float,
        idle_seconds: float,
    ) -> None:
        """Release an unchanged owned process after a monotonic idle deadline."""
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining > 0.0:
                    await asyncio.sleep(remaining)

                async with self._synthesis_lock:
                    if generation != self._idle_generation:
                        return
                    if (
                        self._server_process is not process
                        or process.returncode is not None
                    ):
                        return
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining > 0.0:
                        continue
                    logger.info(
                        "TTS 插件自有服务闲置到期，释放模型资源: "
                        f"idle_seconds={idle_seconds:.1f}"
                    )
                    await self._stop_owned_server_process(
                        reason="idle_timeout",
                        expected_process=process,
                    )
                    return
        except asyncio.CancelledError:
            raise
        finally:
            current = asyncio.current_task()
            task_info = self._idle_shutdown_task
            if task_info is not None and task_info.task is current:
                self._idle_shutdown_task = None

    @asynccontextmanager
    async def _synthesis_activity(self) -> AsyncIterator[None]:
        """Serialize one expression and reset its owned-process idle deadline."""
        await self._cancel_idle_shutdown_task()
        async with self._synthesis_lock:
            try:
                yield
            finally:
                self._arm_idle_shutdown()

    @staticmethod
    def _signal_server_process(
        process: asyncio.subprocess.Process,
        sig: signal.Signals,
    ) -> None:
        """Signal one process that this service started, including its workers."""
        if os.name == "posix":
            os.killpg(process.pid, sig)
            return
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()

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
                "voice": style_cfg.voice,
                "aux_refer_wav_paths": list(getattr(style_cfg, "aux_refer_wav_paths", None) or []),
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
    # 参考音频校验
    # ------------------------------------------------------------------

    # 历史 GPT-SoVITS 后端曾要求 3~10 秒；当前 IndexTTS2 兼容层没有这个硬限制。
    # 常量只用于给历史部署提供可观测提示，不再在通用客户端提前拒绝请求。
    MAIN_REF_MIN_SECONDS: float = 3.0
    MAIN_REF_MAX_SECONDS: float = 10.0

    @staticmethod
    def _probe_audio_duration(path: str) -> float | None:
        """读取音频时长（秒）。无法解析时返回 None。"""
        try:
            info = sf.info(path)
            if not info.samplerate:
                return None
            return info.frames / float(info.samplerate)
        except Exception as e:
            logger.warning(f"无法解析参考音频时长 ({path}): {e}")
            return None

    def _validate_main_ref_duration(self, ref_wav_path: str) -> bool:
        """校验主参考音频存在，并观察历史协议的时长兼容性。

        Args:
            ref_wav_path: 主参考音频路径

        Returns:
            文件存在时返回 True；具体时长能力由当前本地后端判定。
        """
        if not os.path.isfile(ref_wav_path):
            logger.error(f"主参考音频不存在: {ref_wav_path}")
            return False

        duration = self._probe_audio_duration(ref_wav_path)
        if duration is None:
            return True

        if not (self.MAIN_REF_MIN_SECONDS <= duration <= self.MAIN_REF_MAX_SECONDS):
            logger.info(
                "主参考音频不在历史 GPT-SoVITS 3~10 秒兼容区间内；"
                f"交由当前本地后端判定: duration={duration:.2f}s"
            )

        return True

    @staticmethod
    def _audio_file_to_data_url(path: str) -> str:
        """Read one immutable request reference as a base64 data URL."""
        mime_type = mimetypes.guess_type(path)[0] or "audio/wav"
        with open(path, "rb") as audio_file:
            encoded = base64.b64encode(audio_file.read()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _resolve_aux_ref_paths(self, aux_paths: list[str] | None) -> list[str]:
        """筛选出真实存在的辅助参考音频路径。

        辅助参考音频只参与音色融合，不受主参考音频的时长限制。

        Args:
            aux_paths: 配置中的辅助参考音频路径列表

        Returns:
            去重后确实存在的路径列表
        """
        if not aux_paths:
            return []

        resolved: list[str] = []
        for path in aux_paths:
            candidate = (path or "").strip()
            if not candidate or candidate in resolved:
                continue
            if not os.path.isfile(candidate):
                logger.warning(f"辅助参考音频不存在，已跳过: {candidate}")
                continue
            resolved.append(candidate)

        if resolved:
            logger.info(f"启用辅助参考音频 {len(resolved)} 条用于音色融合。")
        return resolved

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

    @staticmethod
    def _is_decorative_speech_symbol(char: str) -> bool:
        """Return whether one code point decorates text but has no spoken form."""
        codepoint = ord(char)
        return (
            unicodedata.category(char) == "So"
            or char in {"\ufe0f", "\u200d"}
            or 0x1F3FB <= codepoint <= 0x1F3FF
        )

    @classmethod
    def _project_decorative_speech_boundaries(cls, text: str) -> str:
        """Turn an interior decorative run into a short spoken boundary.

        Decorative symbols remain present in the authoritative message. This
        helper only prevents their removal from accidentally joining two spoken
        clauses. It must not promote a decorative beat to a sentence break.
        """
        projected: list[str] = []
        index = 0
        while index < len(text):
            char = text[index]
            if not cls._is_decorative_speech_symbol(char):
                projected.append(char)
                index += 1
                continue

            while index < len(text) and cls._is_decorative_speech_symbol(text[index]):
                index += 1
            next_index = index
            while next_index < len(text) and text[next_index].isspace():
                next_index += 1
            previous = next((item for item in reversed(projected) if not item.isspace()), "")
            following = text[next_index] if next_index < len(text) else ""
            if previous.isalnum() and following.isalnum():
                while projected and projected[-1].isspace():
                    projected.pop()
                projected.append("，")

        return "".join(projected)

    def _clean_text_for_tts(self, text: str) -> str:
        """清洗文本以适合 TTS 合成。

        Args:
            text: 原始文本

        Returns:
            清洗后的文本
        """
        # 0. 移除 Higgs 风格控制标记（<|emotion:...|> / <|style:...|> / <|sfx:...|> 等）。
        #    GPT-SoVITS 不理解这类标记，若不先剥离，后续字符过滤会把尖括号和竖线删掉，
        #    只留下 "emotion:affection" 这类裸文本被当成正文念出来。
        text = re.sub(r"<\s*\|[^|>]*\|\s*>", "", text)
        #    兜底：容忍缺失闭合符号的半个标记（如 "<|emotion:affection"）。
        #    只吃 ASCII 标记体（字母/数字/下划线/冒号/连字符/点/空格），
        #    避免贪婪匹配把标记后面的正文一起删掉。
        text = re.sub(r"<\s*\|[\w:\-.\s]*(?:\|\s*>|>|(?=\s)|$)", "", text)

        # 1. 基本清理。GPT-SoVITS 的全角波浪号与省略号属于已经人工
        #    验收的韵律合同；只把 ASCII 三点规范为同一省略号，不把
        #    作者选择的短停顿擅自提升为句号。
        text = re.sub(r"[\(（\[【].*?[\)）\]】]", "", text)
        text = re.sub(r"\.{3,}", "……", text)

        # 2. 词语替换
        replacements = {"www": "哈哈哈", "hhh": "哈哈", "233": "哈哈", "666": "厉害", "88": "拜拜"}
        for old, new in replacements.items():
            text = text.replace(old, new)

        text = self._project_decorative_speech_boundaries(text)
        # 3. 移除不必要的字符
        text = re.sub(
            r"[^\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ffa-zA-Z0-9\s，。！？、；：,.!?;:~～…]",
            "",
            text,
        )

        # 4. 生成非权威的可发音投影。保留已人工验收的波浪号和省略号
        #    韵律；原始消息和训练轨迹始终保留完整展示文本。
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"\s*([。！？!?])\s*", r"\1", text)
        text = re.sub(r"\s*([，、；：,;:])\s*", r"\1", text)
        text = re.sub(
            r"([，。！？、；：,.!?;:\-`])\1+",
            r"\1",
            text,
        )

        # 5. 确保结尾有标点
        if text and not text.endswith(tuple("，。！？、；：,.!?;:~～…")):
            text += "。"

        return text.strip()

    @staticmethod
    def _estimate_synthesis_units(text: str) -> int:
        """Estimate model pressure without pretending this is a tokenizer.

        CJK/Kana/Hangul characters count individually. Consecutive Latin letters
        and digits count at roughly four characters per unit. Punctuation counts
        as one unit, while whitespace only separates runs.
        """
        units = 0
        latin_run = 0

        def flush_latin() -> None:
            nonlocal latin_run, units
            if latin_run:
                units += math.ceil(latin_run / 4)
                latin_run = 0

        for character in text:
            if character.isascii() and character.isalnum():
                latin_run += 1
                continue
            flush_latin()
            if not character.isspace():
                units += 1
        flush_latin()
        return units

    @staticmethod
    def _boundary_kind(marker: str) -> str:
        """Map visible punctuation to a technical pause class."""
        if "\n" in marker or "\r" in marker:
            return "paragraph"
        if any(character in marker for character in "。！？!?。."):
            return "sentence"
        if any(character in marker for character in "；;：:"):
            return "clause"
        return "phrase"

    @staticmethod
    def _join_spoken_text(left: str, right: str) -> str:
        """Join two spoken fragments without fusing Latin words."""
        if not left:
            return right
        if not right:
            return left
        needs_space = left[-1].isascii() and left[-1].isalnum()
        needs_space = needs_space and right[0].isascii() and right[0].isalnum()
        return f"{left} {right}" if needs_space else f"{left}{right}"

    def _scan_semantic_parts(self, text: str) -> list[tuple[str, str]]:
        """Split at authored punctuation while retaining its spoken order."""
        parts: list[tuple[str, str]] = []
        cursor = 0
        for match in _SEMANTIC_BOUNDARY_RE.finditer(text):
            raw = text[cursor : match.end()]
            boundary = self._boundary_kind(match.group(0))
            spoken = re.sub(r"\s*\r?\n+\s*", " ", raw).strip()
            if spoken:
                parts.append((spoken, boundary))
            elif boundary == "paragraph" and parts:
                previous_text, _previous_boundary = parts[-1]
                parts[-1] = (previous_text, "paragraph")
            cursor = match.end()

        tail = text[cursor:].strip()
        if tail:
            parts.append((tail, "sentence"))
        return parts

    def _split_oversized_part(
        self,
        text: str,
        boundary: str,
        max_units: int,
    ) -> list[tuple[str, str]]:
        """Split one punctuation-free oversized part on a stable UTF-8 boundary."""
        source = text.strip()
        result: list[tuple[str, str]] = []
        start = 0
        while start < len(source):
            units = 0
            latin_run = 0
            last_soft_break = start
            overflow_at: int | None = None
            for index in range(start, len(source)):
                character = source[index]
                if character.isascii() and character.isalnum():
                    latin_run += 1
                    if latin_run % 4 == 1:
                        units += 1
                else:
                    latin_run = 0
                    if not character.isspace():
                        units += 1
                if character.isspace() and units <= max_units:
                    last_soft_break = index + 1
                if units > max_units:
                    overflow_at = index
                    break

            if overflow_at is None:
                tail = source[start:].strip()
                if tail:
                    result.append((tail, boundary))
                break

            hard_limit = max(start + 1, overflow_at)
            minimum_soft_break = start + max(1, (hard_limit - start) // 2)
            cut = (
                last_soft_break
                if minimum_soft_break <= last_soft_break <= hard_limit
                else hard_limit
            )
            fragment = source[start:cut].strip()
            if not fragment:
                fragment = source[start:hard_limit]
                cut = hard_limit
            result.append((fragment, "hard"))
            start = cut
            while start < len(source) and source[start].isspace():
                start += 1
        return result

    def _split_text_for_synthesis(self, text: str) -> list[_SynthesisSegment]:
        """Build a bounded, ordered transport plan for one expression."""
        max_units = int(self._config.tts.segment_max_units)
        total_units = self._estimate_synthesis_units(text)
        if (
            not self._config.tts.long_text_split_enabled
            or total_units <= max_units
        ):
            return [_SynthesisSegment(text=text, boundary="sentence", units=total_units)]

        atomic_parts: list[tuple[str, str]] = []
        for part_text, boundary in self._scan_semantic_parts(text):
            atomic_parts.extend(
                self._split_oversized_part(part_text, boundary, max_units)
            )

        planned: list[_SynthesisSegment] = []
        current_text = ""
        current_boundary = "phrase"
        flush_threshold = max(1, math.floor(max_units * 0.75))

        def flush() -> None:
            nonlocal current_text, current_boundary
            if not current_text:
                return
            planned.append(
                _SynthesisSegment(
                    text=current_text,
                    boundary=current_boundary,
                    units=self._estimate_synthesis_units(current_text),
                )
            )
            current_text = ""
            current_boundary = "phrase"

        for part_text, boundary in atomic_parts:
            candidate = self._join_spoken_text(current_text, part_text)
            if current_text and self._estimate_synthesis_units(candidate) > max_units:
                flush()
                candidate = part_text
            current_text = candidate
            current_boundary = boundary
            current_units = self._estimate_synthesis_units(current_text)
            if boundary in {"sentence", "paragraph", "hard"} or current_units >= flush_threshold:
                flush()
        flush()

        # Very short adjacent pieces may share one bounded backend call. Paragraphs
        # remain independent; punctuation and authored order stay in the text.
        minimum_units = min(int(self._config.tts.segment_min_units), max_units)
        merged: list[_SynthesisSegment] = []
        for segment in planned:
            if (
                merged
                and merged[-1].boundary != "paragraph"
                and min(merged[-1].units, segment.units) < minimum_units
            ):
                combined_text = self._join_spoken_text(merged[-1].text, segment.text)
                combined_units = self._estimate_synthesis_units(combined_text)
                if combined_units <= max_units:
                    merged[-1] = _SynthesisSegment(
                        text=combined_text,
                        boundary=segment.boundary,
                        units=combined_units,
                    )
                    continue
            merged.append(segment)

        if not merged:
            return [_SynthesisSegment(text=text, boundary="sentence", units=total_units)]
        return merged

    def _build_synthesis_plan(
        self,
        text: str,
    ) -> tuple[str, list[_SynthesisSegment]]:
        """Select the single segmentation owner for one outward expression."""

        if self._backend_name() == "legacy_compat":
            return (
                "legacy_native",
                [
                    _SynthesisSegment(
                        text=text,
                        boundary="sentence",
                        units=self._estimate_synthesis_units(text),
                    )
                ],
            )
        return "bounded_transport", self._split_text_for_synthesis(text)

    def _pause_after_segment_ms(self, boundary: str) -> int:
        """Return configured silence for one internal boundary."""
        cfg = self._config.tts
        return {
            "phrase": cfg.phrase_pause_ms,
            "hard": cfg.phrase_pause_ms,
            "clause": cfg.clause_pause_ms,
            "sentence": cfg.sentence_pause_ms,
            "paragraph": cfg.paragraph_pause_ms,
        }.get(boundary, cfg.phrase_pause_ms)

    def _observe_synthesis_audio(
        self,
        *,
        units: int,
        audio_data: bytes,
        segment_index: int,
        segment_count: int,
        scope: str,
    ) -> None:
        """Record content-free duration and pace evidence for delivered audio."""

        try:
            info = sf.info(io.BytesIO(audio_data))
            duration_seconds = float(info.duration)
        except Exception as exc:  # noqa: BLE001 - telemetry cannot block delivery
            logger.debug(
                "TTS音频时长不可解析: "
                f"scope={scope}, segment={segment_index}/{segment_count}, "
                f"error_type={type(exc).__name__}"
            )
            return
        if duration_seconds <= 0.0:
            return

        units_per_second = units / duration_seconds
        pace_warning = units_per_second > _TTS_CLARITY_WARNING_UNITS_PER_SECOND
        message = (
            "TTS声学观测: "
            f"scope={scope}, segment={segment_index}/{segment_count}, units={units}, "
            f"duration_ms={round(duration_seconds * 1000)}, "
            f"units_per_second={units_per_second:.2f}, "
            f"pace_warning={str(pace_warning).lower()}"
        )
        if pace_warning:
            logger.warning(message)
        else:
            logger.info(message)

    def _join_wav_segments(
        self,
        audio_segments: list[bytes],
        synthesis_segments: list[_SynthesisSegment],
    ) -> bytes:
        """Join complete WAV segments with authored-boundary pauses."""
        if not audio_segments or len(audio_segments) != len(synthesis_segments):
            raise ValueError("TTS segment/audio count mismatch")

        sample_rate: int | None = None
        timeline: list[np.ndarray] = []
        for index, audio_data in enumerate(audio_segments):
            samples, current_rate = sf.read(
                io.BytesIO(audio_data),
                dtype="float32",
                always_2d=True,
            )
            if not len(samples):
                raise ValueError(f"TTS segment {index + 1} decoded to empty audio")
            if sample_rate is None:
                sample_rate = int(current_rate)
            elif int(current_rate) != sample_rate:
                raise ValueError("TTS segment sample rates do not match")

            mono = np.mean(samples, axis=1, dtype=np.float32).reshape(-1, 1)
            timeline.append(mono)
            if index < len(audio_segments) - 1:
                pause_ms = self._pause_after_segment_ms(synthesis_segments[index].boundary)
                pause_frames = round(sample_rate * pause_ms / 1000)
                if pause_frames:
                    timeline.append(np.zeros((pause_frames, 1), dtype=np.float32))

        if sample_rate is None:
            raise ValueError("TTS segments did not provide a sample rate")
        combined = np.concatenate(timeline, axis=0)
        with io.BytesIO() as output:
            sf.write(output, combined, sample_rate, format="WAV", subtype="PCM_16")
            return output.getvalue()

    async def _encode_audio(self, wav_audio: bytes, media_type: str) -> bytes:
        """Encode one joined WAV exactly once for the configured platform format."""
        normalized = (media_type or "wav").strip().lower()
        if normalized in {"wav", "wave"}:
            return wav_audio

        transcode_options: dict[str, tuple[str, list[str]]] = {
            "ogg": (
                ".ogg",
                ["-c:a", "libopus", "-b:a", "64k", "-ac", "1", "-ar", "48000"],
            ),
            "opus": (
                ".opus",
                ["-c:a", "libopus", "-b:a", "64k", "-ac", "1", "-ar", "48000"],
            ),
            "mp3": (".mp3", ["-c:a", "libmp3lame", "-b:a", "128k", "-ac", "1"]),
            "aac": (".m4a", ["-c:a", "aac", "-b:a", "128k", "-ac", "1"]),
            "flac": (".flac", ["-c:a", "flac", "-ac", "1"]),
        }
        if normalized not in transcode_options:
            raise ValueError(f"unsupported joined TTS media type: {normalized}")
        output_suffix, codec_args = transcode_options[normalized]
        return await asyncio.to_thread(
            transcode_audio_bytes,
            wav_audio,
            output_suffix=output_suffix,
            codec_args=codec_args,
        )

    # ------------------------------------------------------------------
    # 服务生命周期（健康检查 + 自动拉起）
    # ------------------------------------------------------------------

    def _backend_name(self) -> str:
        """Return the configured transport without guessing from a URL."""
        return str(self._config.tts.backend)

    def _vllm_request_headers(self) -> dict[str, str]:
        """Resolve optional local auth without copying secrets into config/logs."""
        headers: dict[str, str] = {}
        api_key_env = str(self._config.tts.api_key_env or "").strip()
        if not api_key_env:
            return headers
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise RuntimeError("vLLM-Omni TTS auth environment is missing")
        headers["Authorization"] = f"Bearer {api_key}"
        return headers

    @staticmethod
    def _vllm_language_code(text: str, text_language: str) -> str:
        """Map the stable Elysium language choice to IndexTTS2.5 codes."""
        normalized = (text_language or "zh").strip().lower()
        if normalized == "auto_yue":
            return "yue"
        if normalized == "auto":
            return "zh"
        if normalized == "zh":
            has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
            has_latin = bool(re.search(r"[A-Za-z]", text))
            if has_cjk and has_latin:
                return "zhen"
        return normalized

    def _build_vllm_omni_payload(
        self,
        *,
        server_config: dict[str, Any],
        text: str,
        text_language: str,
        response_format: str,
        reference_audio_data_url: str | None,
    ) -> dict[str, Any]:
        """Build one official IndexTTS2.5 OpenAI-compatible request."""
        model = str(self._config.tts.model or "").strip()
        if not model:
            raise ValueError("vLLM-Omni TTS model is not configured")

        payload: dict[str, Any] = {
            "model": model,
            "input": text,
            "response_format": response_format,
            "speed": float(server_config.get("speed_factor", 1.0)),
            "extra_params": {
                "lang": self._vllm_language_code(text, text_language),
                "text_normalization": bool(
                    self._config.tts_advanced.text_normalization
                ),
            },
        }
        voice = str(server_config.get("voice") or "").strip()
        if voice:
            payload["voice"] = voice
        elif reference_audio_data_url:
            payload["ref_audio"] = reference_audio_data_url
        else:
            raise ValueError("vLLM-Omni requires a named voice or reference audio")
        return payload

    async def _is_server_alive(self, base_url: str) -> bool:
        """快速探测 TTS 服务是否存活。"""
        normalized_base = base_url.rstrip("/")
        health_url = (
            f"{normalized_base}/v1/models"
            if self._backend_name() == "vllm_omni"
            else normalized_base
        )
        try:
            headers = (
                self._vllm_request_headers()
                if self._backend_name() == "vllm_omni"
                else {}
            )
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.get(health_url, headers=headers) as resp:
                    if self._backend_name() == "vllm_omni":
                        return resp.status == 200
                    # 历史后端没有统一 health path；任何非 5xx 响应都说明在监听。
                    return resp.status < 500
        except Exception:
            return False

    async def _ensure_server_alive(self, base_url: str) -> bool:
        """确保 TTS 服务可用。若未运行且配置了 auto_start，则自动拉起。

        Returns:
            服务是否可用
        """
        if await self._is_server_alive(base_url):
            return True

        async with self._server_start_lock:
            # Another synthesis request may have started the shared backend
            # while this request was waiting for the single-flight lock.
            if await self._is_server_alive(base_url):
                return True
            return await self._start_server(base_url)

    async def _start_server(self, base_url: str) -> bool:
        """Start one configured backend after the single-flight recheck."""

        cfg = self._config.tts
        if not cfg.auto_start:
            logger.error(f"TTS 服务 {base_url} 未运行，且 auto_start 已禁用。")
            return False

        server_dir = cfg.server_dir
        start_command = cfg.start_command
        if not server_dir or not start_command:
            logger.error("自动拉起失败：server_dir 或 start_command 未配置。")
            return False

        if not os.path.isdir(server_dir):
            logger.error(f"自动拉起失败：工作目录不存在: {server_dir}")
            return False

        self._clear_legacy_weight_cache()
        logger.info(f"TTS 服务未运行，正在自动拉起: {start_command} (cwd={server_dir})")
        try:
            self._server_process = await asyncio.create_subprocess_exec(
                *shlex.split(start_command),
                cwd=server_dir,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            logger.error(f"拉起 TTS 服务进程失败: {e}")
            return False

        # 轮询等待服务就绪
        startup_timeout = cfg.startup_timeout
        poll_interval = 2.0
        elapsed = 0.0
        try:
            while elapsed < startup_timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                # 进程已崩溃
                if self._server_process.returncode is not None:
                    stderr_tail = ""
                    if self._server_process.stderr:
                        stderr_bytes = await self._server_process.stderr.read(2048)
                        stderr_tail = stderr_bytes.decode(errors="replace").strip()
                    logger.error(
                        f"TTS 服务进程已退出 (code={self._server_process.returncode})"
                        f"{f': {stderr_tail}' if stderr_tail else ''}"
                    )
                    self._server_process = None
                    return False

                if await self._is_server_alive(base_url):
                    logger.info(f"TTS 服务已就绪（耗时 ~{elapsed:.0f}s）")
                    self._attest_owned_startup_legacy_weights()
                    return True
        except asyncio.CancelledError:
            await self.stop()
            raise

        logger.error(f"TTS 服务在 {startup_timeout}s 内未就绪，放弃等待。")
        await self.stop()
        return False

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
        """Dispatch one bounded transport segment to the configured backend."""
        if self._backend_name() == "vllm_omni":
            return await self._call_vllm_omni_api(
                server_config,
                text,
                text_language,
                **kwargs,
            )
        return await self._call_legacy_tts_api(
            server_config,
            text,
            text_language,
            **kwargs,
        )

    async def _call_vllm_omni_api(
        self,
        server_config: dict[str, Any],
        text: str,
        text_language: str,
        **kwargs: Any,
    ) -> bytes | None:
        """Call IndexTTS2.5 through vLLM-Omni's speech endpoint."""
        base_url = str(server_config["url"]).rstrip("/")
        if not kwargs.get("_server_ready") and not await self._ensure_server_alive(
            base_url
        ):
            return None

        voice = str(server_config.get("voice") or "").strip()
        reference_audio_data_url = kwargs.get("reference_audio_data_url")
        if not voice and not reference_audio_data_url:
            ref_wav_path = str(kwargs.get("refer_wav_path") or "").strip()
            if not ref_wav_path:
                logger.error("vLLM-Omni TTS 调用失败：当前风格缺少命名音色或参考音频")
                return None
            if not self._validate_main_ref_duration(ref_wav_path):
                return None
            try:
                reference_audio_data_url = await asyncio.to_thread(
                    self._audio_file_to_data_url,
                    ref_wav_path,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "读取 vLLM-Omni 参考音频失败: "
                    f"error_type={type(exc).__name__}"
                )
                return None

        response_format = str(kwargs.get("request_media_type") or "wav").lower()
        try:
            payload = self._build_vllm_omni_payload(
                server_config=server_config,
                text=text,
                text_language=text_language,
                response_format=response_format,
                reference_audio_data_url=reference_audio_data_url,
            )
        except ValueError as exc:
            logger.error(f"vLLM-Omni TTS 请求配置无效: error_type={type(exc).__name__}")
            return None

        try:
            headers = {
                "Content-Type": "application/json",
                **self._vllm_request_headers(),
            }
        except RuntimeError:
            logger.error("vLLM-Omni TTS 鉴权环境变量未设置")
            return None

        logger.info(
            "发送 vLLM-Omni TTS 请求: "
            f"chars={len(text)}, language={payload['extra_params']['lang']}, "
            f"segment={kwargs.get('segment_index', 1)}/{kwargs.get('segment_count', 1)}, "
            f"voice_mode={'named' if voice else 'reference'}, media_type={response_format}"
        )

        async def receive_audio(session: aiohttp.ClientSession) -> bytes | None:
            async with session.post(
                f"{base_url}/v1/audio/speech",
                json=payload,
                headers=headers,
            ) as response:
                if response.status != 200:
                    error_body = await response.read()
                    logger.error(
                        "vLLM-Omni TTS 请求失败: "
                        f"status={response.status}, response_bytes={len(error_body)}"
                    )
                    return None
                audio_data = bytearray()
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    audio_data.extend(chunk)
                if not audio_data:
                    logger.error("vLLM-Omni TTS 返回空音频")
                    return None
                logger.info(f"成功接收 vLLM-Omni 音频，大小: {len(audio_data)} 字节")
                return bytes(audio_data)

        try:
            shared_session = kwargs.get("_http_session")
            if shared_session is not None:
                return await receive_audio(shared_session)

            connector = aiohttp.TCPConnector(
                limit=max(1, int(self._config.tts.segment_concurrency))
            )
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            ) as session:
                return await receive_audio(session)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.error("vLLM-Omni TTS 请求超时")
            return None
        except Exception as exc:
            logger.error(
                "vLLM-Omni TTS 调用异常: "
                f"error_type={type(exc).__name__}"
            )
            return None

    def _clear_legacy_weight_cache(self) -> None:
        """Forget process-local weight state whenever ownership changes."""
        self._legacy_weight_cache_owner = None
        self._legacy_active_weights.clear()
        self._legacy_weight_cache_source = None

    @staticmethod
    def _legacy_weight_identity(weights_path: str) -> _LegacyWeightIdentity | None:
        """Return a symlink-aware, content-free identity for one checkpoint."""
        path = str(weights_path).strip()
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return (os.path.realpath(path), int(stat.st_size), int(stat.st_mtime_ns))

    def _attest_owned_startup_legacy_weights(self) -> bool:
        """Seed weight state only for an explicitly attested owned startup."""
        process = self._server_process
        if (
            self._backend_name() != "legacy_compat"
            or not self._config.tts.legacy_owned_startup_weights_ready
            or process is None
            or process.returncode is not None
        ):
            return False

        default_style = self.tts_styles.get("default") or {}
        identities: dict[str, _LegacyWeightIdentity] = {}
        for weight_type, key in (("gpt", "gpt_weights"), ("sovits", "sovits_weights")):
            raw_path = str(default_style.get(key) or "").strip()
            identity = self._legacy_weight_identity(raw_path) if raw_path else None
            if identity is None:
                self._clear_legacy_weight_cache()
                logger.warning(
                    "TTS 自有进程启动权重声明无效，将执行显式权重切换: "
                    f"weight_type={weight_type}"
                )
                return False
            identities[weight_type] = identity

        self._legacy_weight_cache_owner = process
        self._legacy_active_weights = identities
        self._legacy_weight_cache_source = "owned_startup_attestation"
        logger.info(
            "TTS 自有进程启动权重已声明并绑定: "
            f"pid={process.pid}, weight_count={len(identities)}"
        )
        return True

    @staticmethod
    def _legacy_weight_filename(weights_path: str) -> str:
        """Return a content-free weight label for logs."""
        name = os.path.basename(str(weights_path).strip())
        return name or "unnamed"

    def _legacy_weight_file_ready(self, weights_path: str | None, weight_type: str) -> bool:
        """Fail closed when a configured GPT-SoVITS weight file is missing."""
        if not weights_path or not str(weights_path).strip():
            return True
        path = str(weights_path).strip()
        if os.path.isfile(path):
            return True
        logger.error(
            f"切换 {weight_type} 模型失败: 权重文件不存在 "
            f"name={self._legacy_weight_filename(path)}"
        )
        return False

    async def _switch_legacy_model_weights(
        self,
        base_url: str,
        weights_path: str | None,
        weight_type: str,
    ) -> bool:
        """Load one GPT or SoVITS checkpoint. Refusal must stop synthesis."""
        if not weights_path or not str(weights_path).strip():
            return True
        path = str(weights_path).strip()
        if not self._legacy_weight_file_ready(path, weight_type):
            return False
        identity = self._legacy_weight_identity(path)
        if identity is None:
            return False

        process = self._server_process
        if (
            process is not None
            and process.returncode is None
            and self._legacy_weight_cache_owner is process
            and self._legacy_active_weights.get(weight_type) == identity
        ):
            logger.info(
                "TTS 权重已在当前自有进程中加载，跳过重复切换: "
                f"weight_type={weight_type}, source={self._legacy_weight_cache_source}"
            )
            return True

        api_endpoint = f"/set_{weight_type}_weights"
        switch_url = f"{base_url}{api_endpoint}"
        filename = self._legacy_weight_filename(path)
        started_at = asyncio.get_running_loop().time()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as session:
                async with session.get(switch_url, params={"weights_path": path}) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            f"切换 {weight_type} 模型失败: {resp.status} "
                            f"name={filename} detail={error_text}"
                        )
                        return False
                    current = self._server_process
                    if current is not None and current.returncode is None:
                        if self._legacy_weight_cache_owner is not current:
                            self._clear_legacy_weight_cache()
                            self._legacy_weight_cache_owner = current
                        self._legacy_active_weights[weight_type] = identity
                        self._legacy_weight_cache_source = "confirmed_switch"
                    elapsed_ms = (asyncio.get_running_loop().time() - started_at) * 1000.0
                    logger.info(
                        f"成功切换 {weight_type} 模型: name={filename}, "
                        f"elapsed_ms={elapsed_ms:.1f}"
                    )
                    return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                f"请求切换 {weight_type} 模型时发生网络异常: "
                f"name={filename} error_type={type(exc).__name__}"
            )
            return False

    async def _call_legacy_tts_api(
        self,
        server_config: dict[str, Any],
        text: str,
        text_language: str,
        **kwargs: Any,
    ) -> bytes | None:
        """调用本地 TTS API 进行语音合成。

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
            logger.error("TTS API 调用失败：当前风格缺少 refer_wav_path")
            return None

        # 通用客户端只验证文件存在；IndexTTS2 与历史兼容后端各自判定时长能力。
        if not self._validate_main_ref_duration(ref_wav_path):
            return None

        loop = asyncio.get_running_loop()
        request_started_at = loop.time()
        aux_ref_paths = self._resolve_aux_ref_paths(kwargs.get("aux_refer_wav_paths"))

        try:
            base_url = server_config["url"].rstrip("/")

            # 确保 TTS 服务存活（必要时自动拉起）
            if not await self._ensure_server_alive(base_url):
                return None
            server_ready_at = loop.time()

            # Validate the complete pair before mutating either model. A missing
            # second checkpoint must not leave the live process half-switched.
            requested_weights = (
                (kwargs.get("gpt_weights"), "gpt"),
                (kwargs.get("sovits_weights"), "sovits"),
            )
            if not all(self._legacy_weight_file_ready(path, kind) for path, kind in requested_weights):
                return None

            # 步骤一：切换模型权重。缺文件或非 200 必须停止，禁止用进程内残留权重继续合成。
            if not await self._switch_legacy_model_weights(
                base_url, kwargs.get("gpt_weights"), "gpt"
            ):
                return None
            if not await self._switch_legacy_model_weights(
                base_url, kwargs.get("sovits_weights"), "sovits"
            ):
                return None
            weights_ready_at = loop.time()

            # 步骤二：构建合成请求数据
            data: dict[str, Any] = {
                "text": text,
                "text_lang": text_language,
                "ref_audio_path": ref_wav_path,
                "prompt_text": kwargs.get("prompt_text", ""),
                "prompt_lang": kwargs.get("prompt_language", "zh"),
            }

            # 辅助参考音频只参与音色融合，不受 3~10 秒限制。
            if aux_ref_paths:
                data["aux_ref_audio_paths"] = aux_ref_paths

            # 合并高级配置
            cfg = self._config
            advanced_dict = cfg.tts_advanced.model_dump()
            data.update(
                {
                    k: v
                    for k, v in advanced_dict.items()
                    if v is not None and k != "text_normalization"
                }
            )
            request_media_type = kwargs.get("request_media_type")
            if request_media_type:
                # Multi-segment synthesis always obtains lossless WAV chunks.
                # The joined expression is encoded once after all chunks succeed.
                data["media_type"] = request_media_type

            # 优先使用风格特定的语速
            if server_config.get("speed_factor") is not None:
                data["speed_factor"] = server_config["speed_factor"]

            # 步骤三：发送合成请求
            tts_url = base_url if base_url.endswith("/tts") else f"{base_url}/tts"
            logger.info(
                "发送本地 TTS 请求: "
                f"chars={len(text)}, language={text_language}, "
                f"segment={kwargs.get('segment_index', 1)}/{kwargs.get('segment_count', 1)}, "
                f"aux_refs={len(aux_ref_paths)}, media_type={data.get('media_type', 'wav')}"
            )

            connector = aiohttp.TCPConnector(limit=100)
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.post(tts_url, json=data) as response:
                    if response.status == 200:
                        audio_data = bytearray()
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            audio_data.extend(chunk)
                        completed_at = loop.time()
                        logger.info(
                            "TTS legacy 调用完成: "
                            f"server_wait_ms={(server_ready_at - request_started_at) * 1000.0:.1f}, "
                            f"weight_ms={(weights_ready_at - server_ready_at) * 1000.0:.1f}, "
                            f"synthesis_ms={(completed_at - weights_ready_at) * 1000.0:.1f}, "
                            f"total_ms={(completed_at - request_started_at) * 1000.0:.1f}, "
                            f"audio_bytes={len(audio_data)}"
                        )
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
        if len(clean_text) > self.max_text_length:
            logger.error(
                "TTS完整表达超过配置上限，拒绝静默截断: "
                f"chars={len(clean_text)}, max_chars={self.max_text_length}"
            )
            raise ValueError(
                "TTS expression exceeds max_text_length: "
                f"chars={len(clean_text)}, max_chars={self.max_text_length}"
            )

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
            "开始本地 TTS 语音合成: "
            f"chars={len(clean_text)}, style={style}, language={final_language}"
        )
        backend = self._backend_name()
        plan_mode, synthesis_segments = self._build_synthesis_plan(clean_text)
        logger.info(
            "TTS内部合成计划已建立: "
            f"mode={plan_mode}, segments={len(synthesis_segments)}, total_units="
            f"{sum(segment.units for segment in synthesis_segments)}, "
            f"max_segment_units={max(segment.units for segment in synthesis_segments)}"
        )

        call_kwargs = {
            "refer_wav_path": server_config.get("refer_wav_path"),
            "prompt_text": server_config.get("prompt_text"),
            "prompt_language": server_config.get("prompt_language"),
            "aux_refer_wav_paths": server_config.get("aux_refer_wav_paths"),
            "gpt_weights": server_config.get("gpt_weights"),
            "sovits_weights": server_config.get("sovits_weights"),
        }
        final_media_type = self._config.tts_advanced.media_type
        spatial_enabled = self._config.spatial_effects.enabled

        # Reference audio is immutable for one outward expression. Encode it
        # once before bounded parallel segment requests instead of once per part.
        if backend == "vllm_omni" and not str(server_config.get("voice") or "").strip():
            ref_wav_path = str(server_config.get("refer_wav_path") or "").strip()
            if not ref_wav_path or not self._validate_main_ref_duration(ref_wav_path):
                return None
            try:
                call_kwargs["reference_audio_data_url"] = await asyncio.to_thread(
                    self._audio_file_to_data_url,
                    ref_wav_path,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "读取 vLLM-Omni 参考音频失败: "
                    f"error_type={type(exc).__name__}"
                )
                return None

        # One lock covers the complete outward expression. vLLM-Omni may batch
        # its internal transport segments, but two visible expressions never mix.
        async with self._synthesis_activity():
            if (
                backend == "legacy_compat"
                and len(synthesis_segments) == 1
                and not spatial_enabled
            ):
                audio_data = await self._call_tts_api(
                    server_config=server_config,
                    text=synthesis_segments[0].text,
                    text_language=final_language,
                    segment_index=1,
                    segment_count=1,
                    **call_kwargs,
                )
                if not audio_data:
                    return None
                self._observe_synthesis_audio(
                    units=synthesis_segments[0].units,
                    audio_data=audio_data,
                    segment_index=1,
                    segment_count=1,
                    scope="legacy_native_expression",
                )
                return base64.b64encode(audio_data).decode("utf-8")

            async def synthesize_segment(
                index: int,
                segment: _SynthesisSegment,
                request_kwargs: dict[str, Any],
                slots: asyncio.Semaphore | None = None,
            ) -> bytes | None:
                async def call_backend() -> bytes | None:
                    return await self._call_tts_api(
                        server_config=server_config,
                        text=segment.text,
                        text_language=final_language,
                        request_media_type="wav",
                        segment_index=index,
                        segment_count=len(synthesis_segments),
                        **request_kwargs,
                    )

                if slots is None:
                    audio_data = await call_backend()
                else:
                    async with slots:
                        audio_data = await call_backend()
                if audio_data:
                    self._observe_synthesis_audio(
                        units=segment.units,
                        audio_data=audio_data,
                        segment_index=index,
                        segment_count=len(synthesis_segments),
                        scope=(
                            "legacy_native_expression"
                            if backend == "legacy_compat"
                            else "transport_segment"
                        ),
                    )
                return audio_data

            async def synthesize_all(
                request_kwargs: dict[str, Any],
            ) -> list[bytes | None]:
                if backend == "vllm_omni" and len(synthesis_segments) > 1:
                    slots = asyncio.Semaphore(
                        min(
                            len(synthesis_segments),
                            int(self._config.tts.segment_concurrency),
                        )
                    )
                    return list(
                        await asyncio.gather(
                            *(
                                synthesize_segment(
                                    index,
                                    segment,
                                    request_kwargs,
                                    slots,
                                )
                                for index, segment in enumerate(
                                    synthesis_segments,
                                    start=1,
                                )
                            )
                        )
                    )

                results: list[bytes | None] = []
                for index, segment in enumerate(synthesis_segments, start=1):
                    results.append(
                        await synthesize_segment(index, segment, request_kwargs)
                    )
                return results

            if backend == "vllm_omni":
                base_url = str(server_config["url"]).rstrip("/")
                if not await self._ensure_server_alive(base_url):
                    return None
                connector = aiohttp.TCPConnector(
                    limit=max(1, int(self._config.tts.segment_concurrency))
                )
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                async with aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout,
                ) as session:
                    segment_results = await synthesize_all(
                        {
                            **call_kwargs,
                            "_server_ready": True,
                            "_http_session": session,
                        }
                    )
            else:
                segment_results = await synthesize_all(call_kwargs)

            wav_segments: list[bytes] = []
            for index, segment_audio in enumerate(segment_results, start=1):
                if segment_audio is None:
                    logger.error(
                        "TTS内部片段合成失败，完整表达未交付: "
                        f"failed_segment={index}, segment_count={len(synthesis_segments)}"
                    )
                    return None
                wav_segments.append(segment_audio)

            try:
                if len(wav_segments) == 1:
                    joined_audio = wav_segments[0]
                else:
                    joined_audio = await asyncio.to_thread(
                        self._join_wav_segments,
                        wav_segments,
                        synthesis_segments,
                    )

                if spatial_enabled:
                    logger.info("检测到已启用空间音频效果，开始处理完整表达...")
                    processed_audio = await self._apply_spatial_audio_effect(joined_audio)
                    if processed_audio:
                        joined_audio = processed_audio
                    else:
                        logger.warning("空间音频效果应用失败，将使用未处理的完整表达音频。")

                final_audio = await self._encode_audio(joined_audio, final_media_type)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "TTS完整表达拼接或编码失败，未交付音频: "
                    f"error_type={type(exc).__name__}"
                )
                return None

            return base64.b64encode(final_audio).decode("utf-8")
