"""life_engine 结构化审计日志。

通过统一日志系统写入 SQLite，不再维护独立的文件处理器。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("life_engine.audit")

# 日志存储位置（SQLite）
_LOG_DB_PATH = Path("data/logs.db")


def get_life_log_dir() -> Path:
    """获取日志存储目录。"""
    return _LOG_DB_PATH.parent


def get_life_log_file() -> Path:
    """获取日志存储文件路径（SQLite DB）。"""
    return _LOG_DB_PATH


def setup_life_audit_logger() -> Path:
    """初始化审计日志（现在为 no-op，统一由 kernel logger 管理）。"""
    return _LOG_DB_PATH


def teardown_life_audit_logger() -> None:
    """释放审计日志（现在为 no-op，统一由 kernel logger 管理）。"""
    pass



def _emit(payload: dict[str, Any], *, level: str = "info") -> None:
    """写入一条结构化日志（通过统一 logger 写入 SQLite）。"""
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    getattr(logger, level.lower(), logger.info)(line)


def log_lifecycle(event: str, **fields: Any) -> None:
    """记录生命周期事件。"""
    payload = {
        "component": "life_engine",
        "event": event,
        "kind": "lifecycle",
        **fields,
    }
    _emit(payload)


def log_message_received(**fields: Any) -> None:
    """记录收到的聊天消息。"""
    payload = {
        "component": "life_engine",
        "event": "message_received",
        "kind": "message",
        **fields,
    }
    _emit(payload)


def log_wake_context_injected(**fields: Any) -> None:
    """记录一次唤醒上下文注入。"""
    payload = {
        "component": "life_engine",
        "event": "wake_context_injected",
        "kind": "context",
        **fields,
    }
    _emit(payload)


def log_heartbeat(**fields: Any) -> None:
    """记录一次心跳。"""
    payload = {
        "component": "life_engine",
        "event": "heartbeat",
        "kind": "heartbeat",
        **fields,
    }
    _emit(payload)


def log_heartbeat_model_response(**fields: Any) -> None:
    """记录一次心跳模型回复。"""
    payload = {
        "component": "life_engine",
        "event": "heartbeat_model_response",
        "kind": "heartbeat_model",
        **fields,
    }
    _emit(payload)


def log_error(event: str, error: str, **fields: Any) -> None:
    """记录异常。"""
    payload = {
        "component": "life_engine",
        "event": event,
        "kind": "error",
        "error": error,
        **fields,
    }
    _emit(payload, level="error")
