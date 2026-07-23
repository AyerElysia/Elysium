"""三环自学习/自反思系统。

借鉴 VibeGamer 的假设驱动学习环，为数字生命设计的完整自学习闭环：

- 快环（ReflectionEngine）：交互/内省后提取洞察候选
- 审计环（InsightAuditor）：独立 LLM 验证、偏误检测
- 慢环（SelfKnowledgeCompressor）：压缩为版本化自我认知文档

核心组件：
- InsightStore: 洞察实验账本（append-only 审计日志 + 快照）
- LearningScheduler: 三环调度协调器
- LearningMetrics: 学习曲线追踪
"""

from .models import (
    AuditRecord,
    AuditVerdict,
    BiasType,
    Evidence,
    EvidenceKind,
    Insight,
    InsightCategory,
    InsightNextAction,
    InsightStatus,
    KnowledgeVersion,
    LearningMetricsPoint,
)
from .store import InsightStore
from .reflection import ReflectionEngine
from .auditor import InsightAuditor
from .knowledge import SelfKnowledgeCompressor
from .metrics import LearningMetrics
from .scheduler import LearningScheduler
from .tools import LEARNING_TOOLS

__all__ = [
    # Models
    "AuditRecord",
    "AuditVerdict",
    "BiasType",
    "Evidence",
    "EvidenceKind",
    "Insight",
    "InsightCategory",
    "InsightNextAction",
    "InsightStatus",
    "KnowledgeVersion",
    "LearningMetricsPoint",
    # Core
    "InsightStore",
    "ReflectionEngine",
    "InsightAuditor",
    "SelfKnowledgeCompressor",
    "LearningMetrics",
    "LearningScheduler",
    # Tools
    "LEARNING_TOOLS",
]
