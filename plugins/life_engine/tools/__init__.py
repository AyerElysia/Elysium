"""life_engine 工具集。"""

from __future__ import annotations

from ..narrative.tools import NARRATIVE_TOOLS, LifeEngineWriteNarrativeTool
from ..streams.tools import STREAM_TOOLS
from ..trace.tools import LIFE_TRACE_TOOLS
from .autonomy_tools import (
    AUTONOMY_TOOLS,
    LifeEngineManageAutonomyIntentTool,
    LifeEngineScheduleAutonomyIntentTool,
)
from .chat_history_tools import (
    CHAT_HISTORY_TOOLS,
    LifeEngineFetchChatHistoryTool,
)
from .conversation_evidence import (
    CONVERSATION_EVIDENCE_TOOLS,
    LifeEngineConversationEvidenceTool,
)
from .exec_tools import EXEC_TOOLS
from .file_tools import (
    ALL_TOOLS as FILE_TOOLS,
)
from .file_tools import (
    FetchLifeMemoryTool,
    LifeEngineEditFileTool,
    LifeEngineListFilesTool,
    LifeEngineMakeDirectoryTool,
    LifeEngineReadFileTool,
    LifeEngineRunAgentTool,
    LifeEngineWakeDFCTool,
    LifeEngineWriteFileTool,
)
from .grep_tools import GREP_TOOLS
from .platform_history_sync import (
    PLATFORM_HISTORY_SYNC_TOOLS,
    LifeEngineSyncPlatformHistoryTool,
)
from .platform_tools import PLATFORM_TOOLS, PlatformActionTool
from .rest_tools import REST_TOOLS, LifeEngineRestHeartbeatTool
from .screen_tools import SCREEN_TOOLS, LifeEngineViewScreenTool
from .skill_tools import SKILL_TOOLS, LifeEngineSkillTool
from .todo_tools import TODO_TOOLS
from .web_tools import WEB_TOOLS

ALL_TOOLS = [
    *FILE_TOOLS,
    *LIFE_TRACE_TOOLS,
    *NARRATIVE_TOOLS,
    *CHAT_HISTORY_TOOLS,
    *CONVERSATION_EVIDENCE_TOOLS,
    *PLATFORM_HISTORY_SYNC_TOOLS,
    *REST_TOOLS,
    *SCREEN_TOOLS,
    *EXEC_TOOLS,
    *PLATFORM_TOOLS,
]

__all__ = [
    "ALL_TOOLS",
    "TODO_TOOLS",
    "GREP_TOOLS",
    "WEB_TOOLS",
    "STREAM_TOOLS",
    "REST_TOOLS",
    "SCREEN_TOOLS",
    "AUTONOMY_TOOLS",
    "SKILL_TOOLS",
    "LIFE_TRACE_TOOLS",
    "NARRATIVE_TOOLS",
    "LifeEngineWriteNarrativeTool",
    "LifeEngineFetchChatHistoryTool",
    "LifeEngineConversationEvidenceTool",
    "LifeEngineSyncPlatformHistoryTool",
    "LifeEngineRestHeartbeatTool",
    "LifeEngineViewScreenTool",
    "LifeEngineScheduleAutonomyIntentTool",
    "LifeEngineManageAutonomyIntentTool",
    "LifeEngineSkillTool",
    "LifeEngineReadFileTool",
    "LifeEngineWriteFileTool",
    "LifeEngineEditFileTool",
    "LifeEngineListFilesTool",
    "LifeEngineMakeDirectoryTool",
    "LifeEngineWakeDFCTool",
    "LifeEngineRunAgentTool",
    "FetchLifeMemoryTool",
    "PlatformActionTool",
]
