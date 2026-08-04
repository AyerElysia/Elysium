"""HMAC 签名的不透明 API v1 token 和 cursor。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


class SignedValueError(ValueError):
    """签名值无效、用途错误、过期或版本不受支持。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class DecodedValue:
    """已验证的签名值。"""

    purpose: str
    payload: dict[str, Any]
    expires_at: datetime | None


class SignedValueCodec:
    """生成带用途绑定、版本和可选 TTL 的 HMAC 签名值。"""

    def __init__(self, secret: str | bytes) -> None:
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(raw) < 32:
            raise ValueError("signing secret must contain at least 32 bytes")
        self._secret = raw

    def encode(
        self,
        *,
        purpose: str,
        payload: dict[str, Any],
        ttl: timedelta | None = None,
        now: datetime | None = None,
    ) -> str:
        """签名一个规范 JSON envelope。"""

        current = self._utc(now)
        envelope: dict[str, Any] = {
            "version": 1,
            "purpose": purpose,
            "nonce": secrets.token_urlsafe(18),
            "issued_at": current.isoformat(),
            "payload": payload,
        }
        if ttl is not None:
            if ttl <= timedelta(0):
                raise ValueError("ttl must be positive")
            envelope["expires_at"] = (current + ttl).isoformat()
        body = self._b64encode(
            json.dumps(
                envelope,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        signature = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
        return f"{body}.{self._b64encode(signature)}"

    def decode(
        self,
        value: str,
        *,
        purpose: str,
        now: datetime | None = None,
    ) -> DecodedValue:
        """验证签名、用途、版本与过期时间。"""

        try:
            body_text, signature_text = value.split(".", 1)
            body = body_text.encode("ascii")
            signature = self._b64decode(signature_text)
            expected = hmac.new(self._secret, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise SignedValueError("signature_invalid")
            envelope = json.loads(self._b64decode(body_text))
        except SignedValueError:
            raise
        except (UnicodeEncodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise SignedValueError("value_invalid") from exc

        if envelope.get("version") != 1:
            raise SignedValueError("version_unsupported")
        if envelope.get("purpose") != purpose:
            raise SignedValueError("purpose_invalid")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise SignedValueError("payload_invalid")
        expires_at_text = envelope.get("expires_at")
        expires_at = None
        if expires_at_text is not None:
            try:
                expires_at = datetime.fromisoformat(expires_at_text)
            except (TypeError, ValueError) as exc:
                raise SignedValueError("expiry_invalid") from exc
            if expires_at.tzinfo is None:
                raise SignedValueError("expiry_invalid")
            if expires_at <= self._utc(now):
                raise SignedValueError("value_expired")
        return DecodedValue(purpose=purpose, payload=payload, expires_at=expires_at)

    def encode_cursor(self, position: int, *, ledger: str) -> str:
        """生成绑定账本且不可篡改的非过期 cursor。"""

        if position < 0:
            raise ValueError("cursor position must not be negative")
        return self.encode(
            purpose="cursor",
            payload={"ledger": ledger, "position": position},
        )

    def decode_cursor(self, value: str, *, ledger: str) -> int:
        """解码 cursor 并拒绝跨账本复用。"""

        decoded = self.decode(value, purpose="cursor")
        if decoded.payload.get("ledger") != ledger:
            raise SignedValueError("cursor_invalid")
        position = decoded.payload.get("position")
        if not isinstance(position, int) or position < 0:
            raise SignedValueError("cursor_invalid")
        return position

    @staticmethod
    def _utc(value: datetime | None) -> datetime:
        current = value or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return current.astimezone(UTC)

    @staticmethod
    def _b64encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @classmethod
    def _b64decode(cls, value: str) -> bytes:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        if cls._b64encode(decoded) != value:
            raise ValueError("non-canonical base64 value")
        return decoded
