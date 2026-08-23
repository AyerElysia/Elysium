"""Subject-level persistent attention threads and instance-local focus."""

from .contracts import (
    AttentionThreadActorInactive,
    AttentionThreadAuthorityPort,
    AttentionThreadCommand,
    AttentionThreadCommit,
    AttentionThreadConflict,
    AttentionThreadEvent,
    AttentionThreadEventPage,
    AttentionThreadPage,
    AttentionThreadPageQuery,
    AttentionThreadProjectionConflict,
    AttentionThreadProjectionItem,
    AttentionThreadTransitionError,
    AttentionThreadValueChunk,
    AttentionThreadView,
    InstanceFocus,
    InstanceFocusPort,
)
from .models import apply_attention_thread_event
from .projection import (
    ATTENTION_THREAD_PROJECTION_ALGORITHM,
    build_attention_thread_projection,
)
from .service import AttentionThreadService

__all__ = [
    "ATTENTION_THREAD_PROJECTION_ALGORITHM",
    "AttentionThreadActorInactive",
    "AttentionThreadAuthorityPort",
    "AttentionThreadCommand",
    "AttentionThreadCommit",
    "AttentionThreadConflict",
    "AttentionThreadEvent",
    "AttentionThreadEventPage",
    "AttentionThreadPage",
    "AttentionThreadPageQuery",
    "AttentionThreadProjectionConflict",
    "AttentionThreadProjectionItem",
    "AttentionThreadService",
    "AttentionThreadTransitionError",
    "AttentionThreadValueChunk",
    "AttentionThreadView",
    "InstanceFocus",
    "InstanceFocusPort",
    "apply_attention_thread_event",
    "build_attention_thread_projection",
]
