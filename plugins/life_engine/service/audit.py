"""life_engine 专属文件日志。"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("life_engine.audit")

LOG_DIR = Path("logs/life_engine")
LOG_FILE = LOG_DIR / "life.log"
_LOGGER_NAME = "life_engine.audit"


class AuditLoggerManager:
    """审计日志管理器（单例）。"""

    _instance: AuditLoggerManager | None = None
    _lock = Lock()

    def __init__(self) -> None:
        """初始化管理器。"""
        self._handler: RotatingFileHandler | None = None
        self._setup_lock = Lock()

    @classmethod
    def get_instance(cls) -> AuditLoggerManager:
        """获取单例实例。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def setup(self) -> Path:
        """初始化日志处理器。"""
        with self._setup_lock:
            if self._handler is not None:
                return LOG_FILE

            LOG_DIR.mkdir(parents=True, exist_ok=True)

            audit_logger = logging.getLogger(_LOGGER_NAME)
            audit_logger.setLevel(logging.INFO)
            audit_logger.propagate = False

            handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=10 * 1024 * 1024,
                backupCount=10,
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
            )

            # 清理旧处理器
            for existing in list(audit_logger.handlers):
                audit_logger.removeHandler(existing)
                try:
                    existing.close()
                except (OSError, ValueError) as e:
                    logger.debug(f"Failed to close old handler: {e}")

            audit_logger.addHandler(handler)
            self._handler = handler
            return LOG_FILE

    def cleanup(self) -> None:
        """清理日志处理器。"""
        with self._setup_lock:
            if self._handler is not None:
                audit_logger = logging.getLogger(_LOGGER_NAME)
                try:
                    audit_logger.removeHandler(self._handler)
                except ValueError as e:
                    logger.debug(f"Handler already removed: {e}")
                try:
                    self._handler.close()
                except (OSError, ValueError) as e:
                    logger.debug(f"Failed to close handler: {e}")
                finally:
                    self._handler = None


def get_life_log_dir() -> Path:
    """获取 life_engine 日志目录。"""
    return LOG_DIR


def get_life_log_file() -> Path:
    """获取 life_engine 日志文件路径。"""
    return LOG_FILE


def setup_life_audit_logger() -> Path:
    """初始化 life_engine 的文件日志处理器。"""
    return AuditLoggerManager.get_instance().setup()


def teardown_life_audit_logger() -> None:
    """释放 life_engine 文件日志处理器。"""
    AuditLoggerManager.get_instance().cleanup()



def _emit(payload: dict[str, Any], *, level: str = "info") -> None:
    """写入一条结构化日志。"""
    setup_life_audit_logger()
    logger = logging.getLogger(_LOGGER_NAME)
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
