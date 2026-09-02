"""生命域存储适配器共享的写重试原语。

收敛各 ``*_adapters.py`` 中逐字节相同的 ``_retryable`` 判定与
有界写重试循环。判定语义、退避节奏（0.02s * attempt）、耗尽时的
AssertionError 文案均与收敛前各副本保持一致，行为零变更。

注意：``memory/mysql.py`` 持有真正独立的变体（MySQL 专属错误码
1062/2013、duplicate entry / lost connection 文案、无退避 sleep），
刻意不并入本模块。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.exc import DBAPIError

_T = TypeVar("_T")

MAX_WRITE_ATTEMPTS = 3
_RETRY_DELAY_BASE_SECONDS = 0.02
_DEADLOCK_CODES = {"1205", "1213"}


def is_retryable_write_error(exc: DBAPIError) -> bool:
    """判定值得有界重试的瞬时写错误（死锁 / 库锁 / 锁等待超时）。"""

    message = str(exc.orig).lower()
    codes = {str(value) for value in getattr(exc.orig, "args", ())}
    return bool(
        _DEADLOCK_CODES & codes
        or "deadlock" in message
        or "database is locked" in message
        or "lock wait timeout" in message
    )


async def run_write_attempts(
    attempt: Callable[[], Awaitable[_T]],
    *,
    exhaustion_message: str,
    max_attempts: int = MAX_WRITE_ATTEMPTS,
    translate: Callable[[DBAPIError], Exception | None] | None = None,
) -> _T:
    """执行有界写重试循环。

    ``attempt`` 负责打开自己的 unit of work，使调用方可以把后端特定
    钩子（如 writer claim 的绑定/清理）留在每次尝试内部；这样单元
    进入与钩子失败同样处于重试覆盖之下，与收敛前的循环语义一致。

    Args:
        attempt: 单次尝试协程工厂；每次重试都会重新调用。
        exhaustion_message: 循环意外耗尽时 AssertionError 的文案
            （各调用方保留原有措辞，便于日志溯源）。
        max_attempts: 最大尝试次数，默认 3。
        translate: 可选的异常翻译钩子；返回非 None 时立即抛出该异常
            （``from None``），优先于重试判定，用于把底层契约违规
            （如 writer claim 失效）翻译为领域异常。
    """

    for index in range(max_attempts):
        try:
            return await attempt()
        except DBAPIError as exc:
            if translate is not None:
                translated = translate(exc)
                if translated is not None:
                    raise translated from None
            if index + 1 >= max_attempts or not is_retryable_write_error(exc):
                raise
            await asyncio.sleep(_RETRY_DELAY_BASE_SECONDS * (index + 1))
    raise AssertionError(exhaustion_message)
