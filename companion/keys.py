"""Persistent key registry for future companion signed-command routes.

This module does not expose host-control routes. It stores owner-approved
device public keys and replay-protection nonces so future command endpoints can
verify a mobile/PWA request before considering any action.
"""

from __future__ import annotations

import secrets
import uuid
import base64
import binascii
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.exc import IntegrityError

from companion.signing import (
    SIGNED_COMMAND_ALGORITHM,
    SIGNED_COMMAND_VERSION,
    SignedCommandEnvelope,
    SignedCommandError,
    SignedCommandResult,
    load_ed25519_public_key,
    verify_signed_command,
)

DEFAULT_COMMAND_SCOPE = "remote_development"
KEY_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def utc_naive(value: datetime | None = None) -> datetime:
    """Return a naive UTC datetime for the repo's existing DateTime columns."""
    from core.database import utcnow_naive

    if value is None:
        return utcnow_naive()
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def normalize_scopes(scopes: str | Iterable[str] | None) -> str:
    if scopes is None:
        return DEFAULT_COMMAND_SCOPE
    if isinstance(scopes, str):
        items = scopes.split(",")
    else:
        items = list(scopes)
    cleaned = sorted({str(scope).strip() for scope in items if str(scope).strip()})
    return ",".join(cleaned) if cleaned else DEFAULT_COMMAND_SCOPE


def parse_scopes(scopes: str | Iterable[str] | None) -> list[str]:
    if scopes is None:
        return [DEFAULT_COMMAND_SCOPE]
    if isinstance(scopes, str):
        items = scopes.split(",")
    else:
        items = list(scopes)
    cleaned = sorted({str(scope).strip() for scope in items if str(scope).strip()})
    return cleaned if cleaned else [DEFAULT_COMMAND_SCOPE]


def normalize_key_id(key_id: str | None, *, generate: bool = False) -> str:
    value = (key_id or (f"cmd_{secrets.token_urlsafe(12)}" if generate else "")).strip()
    if not value:
        raise SignedCommandError("Companion command key id is required")
    if not KEY_ID_RE.fullmatch(value):
        raise SignedCommandError("Companion command key id has invalid characters")
    return value


def public_key_sha256(public_key_b64: str) -> str:
    """SHA-256 fingerprint for display without returning the public key blob."""
    try:
        raw = base64.b64decode(public_key_b64, validate=True)
    except (binascii.Error, TypeError) as exc:
        raise SignedCommandError("Public key must be base64-encoded") from exc
    return hashlib.sha256(raw).hexdigest()


def serialize_device_key(row) -> dict[str, Any]:
    """Safe response shape for companion key registry endpoints."""
    return {
        "id": row.id,
        "key_id": row.key_id,
        "label": row.label,
        "algorithm": row.algorithm,
        "protocol_version": row.protocol_version,
        "scopes": parse_scopes(row.scopes),
        "is_active": bool(row.is_active),
        "public_key_sha256": public_key_sha256(row.public_key_b64),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }


def register_device_key(
    db,
    *,
    owner: str,
    public_key_b64: str,
    label: str = "Companion device",
    key_id: str | None = None,
    scopes: str | Iterable[str] | None = None,
) -> Any:
    """Persist an owner-approved Ed25519 public key for signed commands."""
    from core.database import CompanionDeviceKey

    owner = (owner or "").strip()
    if not owner:
        raise SignedCommandError("Companion key owner is required")
    key_id = normalize_key_id(key_id, generate=True)
    label = (label or "Companion device").strip() or "Companion device"
    label = label[:80]
    load_ed25519_public_key(public_key_b64)

    row = CompanionDeviceKey(
        id=str(uuid.uuid4()),
        owner=owner,
        key_id=key_id,
        label=label,
        public_key_b64=public_key_b64,
        algorithm=SIGNED_COMMAND_ALGORITHM,
        protocol_version=SIGNED_COMMAND_VERSION,
        scopes=normalize_scopes(scopes),
        is_active=True,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise SignedCommandError("Companion command key id already exists for this owner") from exc
    return row


def get_active_device_key(db, *, owner: str, key_id: str) -> Any:
    """Return an active owner-approved command key or raise SignedCommandError."""
    from core.database import CompanionDeviceKey

    owner = (owner or "").strip()
    key_id = normalize_key_id(key_id)
    if not owner:
        raise SignedCommandError("Companion key owner is required")

    row = (
        db.query(CompanionDeviceKey)
        .filter(CompanionDeviceKey.owner == owner)
        .filter(CompanionDeviceKey.key_id == key_id)
        .first()
    )
    if row is None:
        raise SignedCommandError("Companion command key is not approved")
    if not row.is_active or row.revoked_at is not None:
        raise SignedCommandError("Companion command key is revoked")
    if row.algorithm != SIGNED_COMMAND_ALGORITHM:
        raise SignedCommandError("Unsupported companion command key algorithm")
    if row.protocol_version != SIGNED_COMMAND_VERSION:
        raise SignedCommandError("Unsupported companion command key version")
    return row


def list_device_keys(db, *, owner: str, include_inactive: bool = False) -> list[Any]:
    """List owner-approved command keys. Public-key material is serialized separately."""
    from core.database import CompanionDeviceKey

    owner = (owner or "").strip()
    if not owner:
        raise SignedCommandError("Companion key owner is required")
    q = db.query(CompanionDeviceKey).filter(CompanionDeviceKey.owner == owner)
    if not include_inactive:
        q = q.filter(CompanionDeviceKey.is_active == True)  # noqa: E712
        q = q.filter(CompanionDeviceKey.revoked_at == None)  # noqa: E711
    return q.order_by(CompanionDeviceKey.created_at.desc()).all()


def revoke_device_key(db, *, owner: str, key_id: str) -> tuple[Any, int]:
    """Mark one owner-approved command key revoked and clear its old nonces."""
    from core.database import CompanionCommandNonce

    row = get_active_device_key(db, owner=owner, key_id=key_id)
    row.is_active = False
    row.revoked_at = utc_naive()
    nonce_count = (
        db.query(CompanionCommandNonce)
        .filter(CompanionCommandNonce.owner == row.owner)
        .filter(CompanionCommandNonce.key_id == row.key_id)
        .delete(synchronize_session=False)
    )
    db.flush()
    return row, int(nonce_count or 0)


def prune_expired_nonces(db, *, now: datetime | None = None) -> int:
    """Delete expired replay-protection nonces and return the removed count."""
    from core.database import CompanionCommandNonce

    cutoff = utc_naive(now)
    count = (
        db.query(CompanionCommandNonce)
        .filter(CompanionCommandNonce.expires_at <= cutoff)
        .delete(synchronize_session=False)
    )
    db.flush()
    return int(count or 0)


def delete_owner_device_keys(db, owner: str) -> tuple[int, int]:
    """Delete an owner's approved command keys and their remembered nonces."""
    from core.database import CompanionCommandNonce, CompanionDeviceKey

    owner = (owner or "").strip()
    if not owner:
        return 0, 0
    rows = (
        db.query(CompanionDeviceKey.key_id)
        .filter(CompanionDeviceKey.owner == owner)
        .all()
    )
    key_ids = [row[0] for row in rows]
    nonce_count = 0
    if key_ids:
        nonce_count = (
            db.query(CompanionCommandNonce)
            .filter(CompanionCommandNonce.owner == owner)
            .filter(CompanionCommandNonce.key_id.in_(key_ids))
            .delete(synchronize_session=False)
        )
    key_count = (
        db.query(CompanionDeviceKey)
        .filter(CompanionDeviceKey.owner == owner)
        .delete(synchronize_session=False)
    )
    db.flush()
    return int(key_count or 0), int(nonce_count or 0)


def remember_command_nonce(
    db,
    owner: str,
    key_id: str,
    nonce: str,
    expires_at: datetime,
    *,
    now: datetime | None = None,
) -> bool:
    """Store a signed-command nonce. Returns False for replayed nonces."""
    from core.database import CompanionCommandNonce

    owner = (owner or "").strip()
    key_id = (key_id or "").strip()
    nonce = (nonce or "").strip()
    if not owner or not key_id or not nonce:
        return False
    prune_expired_nonces(db, now=now)
    try:
        with db.begin_nested():
            db.add(CompanionCommandNonce(
                owner=owner,
                key_id=key_id,
                nonce=nonce,
                expires_at=utc_naive(expires_at),
                created_at=utc_naive(now),
            ))
            db.flush()
    except IntegrityError:
        return False
    return True


def verify_registered_signed_command(
    db,
    *,
    owner: str,
    method: str,
    path: str,
    body: Any,
    envelope: SignedCommandEnvelope,
    now: datetime | None = None,
) -> SignedCommandResult:
    """Verify a signed command against an approved key and nonce store."""
    key = get_active_device_key(db, owner=owner, key_id=envelope.key_id)

    result = verify_signed_command(
        method=method,
        path=path,
        body=body,
        envelope=envelope,
        public_key_b64=key.public_key_b64,
        remember_nonce=lambda key_id, nonce, expires_at: remember_command_nonce(
            db,
            owner,
            key_id,
            nonce,
            expires_at,
            now=now,
        ),
        now=now,
    )
    key.last_used_at = utc_naive(now)
    db.flush()
    return result
