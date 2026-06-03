"""Compatibility shim for the renamed default_chatter router agent."""

from .router_agent import (  # noqa: F401
    _fit_unreads_to_sub_agent_budget,
    _safe_count_tokens,
    decide_should_respond,
    route_should_respond,
)
