"""Progressively disclosed engineering argument contracts, not workflow policy."""

from __future__ import annotations

from typing import Any

_WORKFLOW_REF = {
    "workflow_id": "exact ID returned by workflow.replace",
    "workflow_revision": "exact positive revision",
    "workflow_sha256": "exact SHA-256 returned by workflow.replace",
}
_REGISTRATION = {
    "provider_id": "installed capability ID",
    "referent_kind": "explicit source type, e.g. workflow (not inferred by code)",
    "referent_id": "exact source ID; may be the chosen workflow ID",
    "referent_revision": "exact source revision",
    "referent_sha256": "exact source SHA-256",
    **_WORKFLOW_REF,
    "schedule": "manual | at | interval",
    "first_due_at": "timezone-aware ISO timestamp for at/interval",
    "interval_seconds": "positive seconds for interval; 0 otherwise",
}
_ACTIONS = {
    "workflow.replace": {
        "capability_id": "catalog capability ID (installation not required)",
        "content": "your exact workflow text, including explicit empty text; "
        "preserved without stripping, rewriting, or template fallback",
        "content_sha256?": "optional SHA-256; computed from exact UTF-8 when omitted",
    },
    "capability.install": _WORKFLOW_REF,
    "capability.reinstall": _WORKFLOW_REF,
    "capability.bind_workflow": _WORKFLOW_REF,
    "capability.pause": {},
    "capability.resume": {},
    "capability.uninstall": {},
    "opportunity.open": _REGISTRATION,
    "opportunity.schedule": _REGISTRATION,
    "opportunity.snooze": {"first_due_at": "new timezone-aware ISO timestamp"},
    "opportunity.pause": {},
    "opportunity.resume": {},
    "opportunity.close": {},
}


def describe_protocol(action: str = "") -> dict[str, Any]:
    """Return one argument schema; a listing never mutates or adopts anything."""
    common = {
        "tool": "nucleus_opportunity_command",
        "target_id": "your stable workflow/opportunity ID, or catalog capability ID",
        "expected_revision": "0 for new; otherwise exact revision last read",
        "reason": "your explanation, preserved exactly; may be empty",
        "identity": "actor/source/tool occurrence are bound by runtime, not arguments",
        "conflict": "read current revision and decide again; no automatic overwrite",
    }
    if not action:
        return {
            "actions": list(_ACTIONS),
            "common": common,
            "next": "query protocol with record_id=one action",
            "workflow": "write an exact version, then explicitly install/bind it",
            "execution": "nucleus_capability_call uses capability_id, operation, arguments; "
            "an opportunity never executes a workflow automatically",
            "operation_parameters": "query operation_schema with record_id=capability_id "
            "and operation=operation name; nucleus_learn accepts action to disclose "
            "its inner operation. Read all chunks before calling.",
        }
    if action not in _ACTIONS:
        raise ValueError("OpportunityProtocolActionUnknown")
    if action == "opportunity.open":
        common["expected_revision"] = "0 for the first registration only"
    elif action.startswith("opportunity."):
        common["expected_revision"] = (
            "exact positive revision of an existing registration; "
            "first registration uses opportunity.open, never schedule/snooze"
        )
    return {
        "action": action,
        "common": common,
        "arguments": dict(_ACTIONS[action]),
        "partial_registration_update": action.startswith("opportunity.")
        and action != "opportunity.open",
        "bind_scope": "provider binding changes future capability calls; existing "
        "opportunity registrations retain their exact workflow until explicitly configured",
    }
