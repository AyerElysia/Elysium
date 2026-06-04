"""life_engine 工具集。"""

from __future__ import annotations

from .file_tools import (
    ALL_TOOLS as FILE_TOOLS,
    LifeEngineReadFileTool,
    LifeEngineWriteFileTool,
    LifeEngineEditFileTool,
    LifeEngineListFilesTool,
    LifeEngineMakeDirectoryTool,
    LifeEngineWakeDFCTool,
    LifeEngineRunAgentTool,
    FetchLifeMemoryTool,
)
from .chat_history_tools import (
    CHAT_HISTORY_TOOLS,
    LifeEngineFetchChatHistoryTool,
)
from .todo_tools import TODO_TOOLS
from .grep_tools import GREP_TOOLS
from .web_tools import WEB_TOOLS
from .rest_tools import REST_TOOLS, LifeEngineRestHeartbeatTool
from .screen_tools import SCREEN_TOOLS, LifeEngineViewScreenTool
from .autonomy_tools import AUTONOMY_TOOLS, LifeEngineScheduleAutonomyIntentTool
from ..trace.tools import LIFE_TRACE_TOOLS
from ..streams.tools import STREAM_TOOLS
from .exec_tools import EXEC_TOOLS

ALL_TOOLS = [
    *FILE_TOOLS,
    *LIFE_TRACE_TOOLS,
    *CHAT_HISTORY_TOOLS,
    *REST_TOOLS,
    *SCREEN_TOOLS,
    *EXEC_TOOLS,
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
    "LIFE_TRACE_TOOLS",
    "LifeEngineFetchChatHistoryTool",
    "LifeEngineRestHeartbeatTool",
    "LifeEngineViewScreenTool",
    "LifeEngineScheduleAutonomyIntentTool",
    "LifeEngineReadFileTool",
    "LifeEngineWriteFileTool",
    "LifeEngineEditFileTool",
    "LifeEngineListFilesTool",
    "LifeEngineMakeDirectoryTool",
    "LifeEngineWakeDFCTool",
    "LifeEngineRunAgentTool",
    "FetchLifeMemoryTool",
]
