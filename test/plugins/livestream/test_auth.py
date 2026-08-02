from __future__ import annotations

from plugins.livestream.auth import TicketAuthority


def test_ticket_is_tamper_evident_and_single_use() -> None:
    authority = TicketAuthority(b"x" * 32, 30)
    ticket = authority.issue()

    assert authority.consume(ticket) is True
    assert authority.consume(ticket) is False
    assert authority.consume(ticket + "tampered") is False
