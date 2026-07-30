"""降级管线模块。

当全双工 Provider 不可用时，使用级联管线：
VAD -> MiMo-V2.5 音频理解 -> 流式文本 -> 切句 -> IndexTTS2 -> 流式音频
"""

from .pipeline import DegradedPipeline

__all__ = ["DegradedPipeline"]
