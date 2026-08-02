"""Short-lived, single-use authentication tickets for Voice Live sockets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


class TicketAuthority:
    """Issue and consume tamper-evident tickets without retaining secrets."""

    def __init__(self, secret: str | bytes, ttl_seconds: int) -> None:
        raw_secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        if not raw_secret:
            raise ValueError("ticket secret must not be empty")
        if ttl_seconds <= 0:
            raise ValueError("ticket TTL must be positive")
        self._secret = raw_secret
        self._ttl_seconds = ttl_seconds
        self._consumed_nonces: dict[str, float] = {}

    def issue(self) -> str:
        expires_at = int(time.time()) + self._ttl_seconds
        payload = json.dumps(
            {"nonce": secrets.token_urlsafe(18), "expires_at": expires_at},
            separators=(",", ":"),
        ).encode("utf-8")
        body = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
        return f"{body.decode('ascii')}.{encoded_signature.decode('ascii')}"

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
        self._consumed_nonces = {
            known_nonce: expiry
            for known_nonce, expiry in self._consumed_nonces.items()
            if expiry >= now
        }
        if expires_at < now or nonce in self._consumed_nonces:
            return False
        self._consumed_nonces[nonce] = expires_at
        return True

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


__all__ = ["TicketAuthority"]
