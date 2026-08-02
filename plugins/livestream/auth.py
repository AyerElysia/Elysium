"""Short-lived, single-use authorization for livestream control surfaces."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


class TicketAuthority:
    """Issue tamper-evident tickets and reject replay within their TTL."""

    def __init__(self, secret: str | bytes, ttl_seconds: int) -> None:
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        if not raw:
            raise ValueError("ticket secret must not be empty")
        if ttl_seconds <= 0:
            raise ValueError("ticket TTL must be positive")
        self._secret = raw
        self._ttl_seconds = ttl_seconds
        self._consumed: dict[str, float] = {}

    def issue(self) -> str:
        body = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "nonce": secrets.token_urlsafe(18),
                    "expires_at": int(time.time()) + self._ttl_seconds,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).rstrip(b"=")
        signature = base64.urlsafe_b64encode(
            hmac.new(self._secret, body, hashlib.sha256).digest()
        ).rstrip(b"=")
        return f"{body.decode('ascii')}.{signature.decode('ascii')}"

    def consume(self, ticket: str) -> bool:
        try:
            body_text, signature_text = ticket.split(".", 1)
            body = body_text.encode("ascii")
            signature = self._decode(signature_text)
            expected = hmac.new(self._secret, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return False
            payload = json.loads(self._decode(body_text))
            nonce = str(payload["nonce"])
            expires_at = float(payload["expires_at"])
        except (UnicodeEncodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False
        now = time.time()
        self._consumed = {
            known: expiry for known, expiry in self._consumed.items() if expiry >= now
        }
        if expires_at < now or nonce in self._consumed:
            return False
        self._consumed[nonce] = expires_at
        return True

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
