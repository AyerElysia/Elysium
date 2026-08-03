"""Offline-first event synchronization primitives.

The package deliberately owns transport guarantees only.  Product code still
decides which events may cross a trust boundary and how received events are
projected into an application.
"""

from .coordinator import SyncCoordinator, SyncRunResult
from .local_store import LocalSyncStore
from .models import PublishResult, SyncEnvelope, SyncStatus
from .mysql_ledger import MySQLLedgerConfig, RemoteMySQLLedger

__all__ = [
    "LocalSyncStore",
    "MySQLLedgerConfig",
    "PublishResult",
    "RemoteMySQLLedger",
    "SyncCoordinator",
    "SyncEnvelope",
    "SyncRunResult",
    "SyncStatus",
]
