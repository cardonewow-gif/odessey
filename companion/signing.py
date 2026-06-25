"""Signed command helpers for future companion host-control routes.

This module does not execute commands and does not expose any route by itself.
It defines the cryptographic envelope a private mobile/PWA client must satisfy
before later control endpoints can safely accept host/code-development actions.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SIGNED_COMMAND_VERSION = 1
SIGNED_COMMAND_ALGORITHM = "ed25519"
MAX_CLOCK_SKEW_SECONDS = 300

SIGNED_COMMAND_HEADERS = {
    "version": "X-Odysseus-Command-Version",
    "key_id": "X-Odysseus-Command-Key-Id",
    "timestamp": "X-Odysseus-Command-Timestamp",
    "nonce": "X-Odysseus-Command-Nonce",
    "signature": "X-Odysseus-Command-Signature",
}


class SignedCommandError(ValueError):
    """Raised when a signed command envelope is malformed or not authorized."""


@dataclass(frozen=True)
class SignedCommandEnvelope:
    key_id: str
    timestamp: str
    nonce: str
    signature: str
    version: int = SIGNED_COMMAND_VERSION


@dataclass(frozen=True)
class SignedCommandResult:
    key_id: str
    nonce: str
    timestamp: datetime
    body_sha256: str
    protocol_version: int


NonceRecorder = Callable[[str, str, datetime], bool]


def canonical_json(value: Any) -> bytes:
    """Canonical JSON bytes used by the signing protocol.

    A route should pass the already-parsed JSON body. ``None`` is treated as an
    empty object so body-less control actions still sign a deterministic value.
    """
    if value is None:
        value = {}
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SignedCommandError("Body must be JSON-serializable") from exc


def body_sha256(body: Any) -> str:
    return hashlib.sha256(canonical_json(body)).hexdigest()


def signing_payload(
    method: str,
    path: str,
    body: Any,
    envelope: SignedCommandEnvelope,
) -> bytes:
    """Canonical payload bytes that mobile clients sign with Ed25519."""
    if envelope.version != SIGNED_COMMAND_VERSION:
        raise SignedCommandError("Unsupported signed command version")
    method = (method or "").strip().upper()
    path = (path or "").strip()
    if not method:
        raise SignedCommandError("HTTP method is required")
    if not path.startswith("/"):
        raise SignedCommandError("Request path must start with /")
    if not envelope.key_id.strip():
        raise SignedCommandError("Command key id is required")
    if not envelope.nonce.strip():
        raise SignedCommandError("Command nonce is required")
    if not envelope.timestamp.strip():
        raise SignedCommandError("Command timestamp is required")

    return canonical_json({
        "body_sha256": body_sha256(body),
        "key_id": envelope.key_id.strip(),
        "method": method,
        "nonce": envelope.nonce.strip(),
        "path": path,
        "timestamp": envelope.timestamp.strip(),
        "v": envelope.version,
    })


def parse_timestamp(value: str) -> datetime:
    raw = (value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SignedCommandError("Command timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SignedCommandError("Command timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def envelope_from_headers(headers: Mapping[str, str]) -> SignedCommandEnvelope:
    """Build an envelope from request headers.

    Works with Starlette/FastAPI ``Headers`` as well as regular dicts.
    """
    def _get(name: str, default: str = "") -> str:
        value = headers.get(name) if hasattr(headers, "get") else None
        if value is not None:
            return str(value)
        lower = name.lower()
        for key, candidate in headers.items():
            if str(key).lower() == lower:
                return str(candidate)
        return default

    raw_version = _get(SIGNED_COMMAND_HEADERS["version"], str(SIGNED_COMMAND_VERSION))
    try:
        version = int(raw_version)
    except ValueError as exc:
        raise SignedCommandError("Command version must be an integer") from exc
    return SignedCommandEnvelope(
        version=version,
        key_id=_get(SIGNED_COMMAND_HEADERS["key_id"]),
        timestamp=_get(SIGNED_COMMAND_HEADERS["timestamp"]),
        nonce=_get(SIGNED_COMMAND_HEADERS["nonce"]),
        signature=_get(SIGNED_COMMAND_HEADERS["signature"]),
    )


def load_ed25519_public_key(public_key_b64: str) -> Ed25519PublicKey:
    """Load a raw 32-byte Ed25519 public key encoded with base64."""
    try:
        raw = base64.b64decode(public_key_b64, validate=True)
    except (binascii.Error, TypeError) as exc:
        raise SignedCommandError("Public key must be base64-encoded") from exc
    if len(raw) != 32:
        raise SignedCommandError("Ed25519 public key must be 32 bytes")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise SignedCommandError("Invalid Ed25519 public key") from exc


def verify_signed_command(
    *,
    method: str,
    path: str,
    body: Any,
    envelope: SignedCommandEnvelope,
    public_key_b64: str,
    remember_nonce: NonceRecorder | None = None,
    now: datetime | None = None,
    max_clock_skew_seconds: int = MAX_CLOCK_SKEW_SECONDS,
) -> SignedCommandResult:
    """Verify an Ed25519 signed command envelope.

    ``remember_nonce`` should atomically store ``(key_id, nonce)`` until the
    provided expiry and return ``False`` when that pair was already seen.
    """
    timestamp = parse_timestamp(envelope.timestamp)
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    skew = abs((now_utc - timestamp).total_seconds())
    if skew > max_clock_skew_seconds:
        raise SignedCommandError("Command timestamp is outside the freshness window")

    public_key = load_ed25519_public_key(public_key_b64)
    try:
        signature = base64.b64decode(envelope.signature, validate=True)
    except (binascii.Error, TypeError) as exc:
        raise SignedCommandError("Signature must be base64-encoded") from exc
    try:
        public_key.verify(signature, signing_payload(method, path, body, envelope))
    except InvalidSignature as exc:
        raise SignedCommandError("Invalid command signature") from exc

    expiry = now_utc + timedelta(seconds=max_clock_skew_seconds)
    if remember_nonce is not None and not remember_nonce(envelope.key_id, envelope.nonce, expiry):
        raise SignedCommandError("Command nonce has already been used")

    return SignedCommandResult(
        key_id=envelope.key_id,
        nonce=envelope.nonce,
        timestamp=timestamp,
        body_sha256=body_sha256(body),
        protocol_version=envelope.version,
    )
