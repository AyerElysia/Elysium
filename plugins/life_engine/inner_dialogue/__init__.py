"""Inner dialogue round-trip: expression sink and heartbeat return.

This package is a rebuildable projection over Life Events. It is not an
outreach authority and does not choose a social surface.
"""

from .protocol import (
    INNER_DIALOGUE_KIND,
    INNER_DIALOGUE_OPEN_LIMIT,
    INNER_DIALOGUE_RETURN_DELIVERY_KIND,
    INNER_DIALOGUE_RETURN_KIND,
    InnerDialogueConflict,
    InnerDialogueLedger,
    InnerDialogueOpenLimitExceeded,
    InnerDialogueRecord,
    InnerDialogueReturnBlocked,
    InnerDialogueReturnRequiresHeartbeat,
    dump_inner_dialogue_payload,
    inner_dialogue_summary,
    parse_inner_dialogue_payload,
)

__all__ = [
    "INNER_DIALOGUE_KIND",
    "INNER_DIALOGUE_OPEN_LIMIT",
    "INNER_DIALOGUE_RETURN_DELIVERY_KIND",
    "INNER_DIALOGUE_RETURN_KIND",
    "InnerDialogueConflict",
    "InnerDialogueLedger",
    "InnerDialogueOpenLimitExceeded",
    "InnerDialogueRecord",
    "InnerDialogueReturnBlocked",
    "InnerDialogueReturnRequiresHeartbeat",
    "dump_inner_dialogue_payload",
    "inner_dialogue_summary",
    "parse_inner_dialogue_payload",
]
