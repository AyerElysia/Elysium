"""数据库异常定义

提供统一的数据库异常系统。
"""

_MYSQL_DISCONNECT_CODES = frozenset({2006, 2013, 2055})


def is_database_disconnect(error: BaseException) -> bool:
    """Return whether an exception chain proves that its connection was lost.

    The predicate is intentionally narrow: callers may replay only operations
    that have their own idempotency boundary. Lock timeouts, integrity errors,
    syntax errors, and generic transaction failures are never classified as
    disconnects.
    """
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if bool(getattr(current, "connection_invalidated", False)):
            return True
        arguments = getattr(current, "args", ())
        if arguments:
            code = arguments[0]
            if isinstance(code, int) and code in _MYSQL_DISCONNECT_CODES:
                return True
        if isinstance(current, ConnectionError | EOFError):
            return True
        current = current.__cause__ or current.__context__
    return False


class DatabaseError(Exception):
    """数据库基础异常"""

    pass


class DatabaseInitializationError(DatabaseError):
    """数据库初始化异常"""

    pass


class DatabaseConnectionError(DatabaseError):
    """数据库连接异常"""

    pass


class DatabaseQueryError(DatabaseError):
    """数据库查询异常"""

    pass


class DatabaseTransactionError(DatabaseError):
    """数据库事务异常"""

    pass
