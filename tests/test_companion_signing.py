"""Tests for the future companion signed-command protocol helpers."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion.signing import (
    MAX_CLOCK_SKEW_SECONDS,
    SIGNED_COMMAND_HEADERS,
    SIGNED_COMMAND_VERSION,
    SignedCommandEnvelope,
    SignedCommandError,
    body_sha256,
    canonical_json,
    envelope_from_headers,
    signing_payload,
    verify_signed_command,
)


NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
TIMESTAMP = "2026-06-08T12:00:00Z"


def _private_key():
    return Ed25519PrivateKey.generate()


def _public_key_b64(private_key):
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _signed_envelope(private_key, *, body=None, method="POST", path="/api/companion/control"):
    envelope = SignedCommandEnvelope(
        key_id="phone-1",
        timestamp=TIMESTAMP,
        nonce="nonce-1",
        signature="",
        version=SIGNED_COMMAND_VERSION,
    )
    signature = private_key.sign(signing_payload(method, path, body or {}, envelope))
    return SignedCommandEnvelope(
        key_id=envelope.key_id,
        timestamp=envelope.timestamp,
        nonce=envelope.nonce,
        signature=base64.b64encode(signature).decode("ascii"),
        version=envelope.version,
    )


def test_canonical_json_and_body_hash_are_stable():
    left = {"b": 2, "a": {"z": 3, "c": 1}}
    right = {"a": {"c": 1, "z": 3}, "b": 2}

    assert canonical_json(left) == canonical_json(right)
    assert body_sha256(left) == body_sha256(right)


def test_verify_signed_command_accepts_valid_ed25519_signature():
    private_key = _private_key()
    body = {"command": "status", "args": {"tail": 20}}
    envelope = _signed_envelope(private_key, body=body)
    seen = set()

    def remember_nonce(key_id, nonce, expires_at):
        assert expires_at.tzinfo is not None
        pair = (key_id, nonce)
        if pair in seen:
            return False
        seen.add(pair)
        return True

    result = verify_signed_command(
        method="POST",
        path="/api/companion/control",
        body=body,
        envelope=envelope,
        public_key_b64=_public_key_b64(private_key),
        remember_nonce=remember_nonce,
        now=NOW,
    )

    assert result.key_id == "phone-1"
    assert result.nonce == "nonce-1"
    assert result.timestamp == NOW
    assert result.body_sha256 == body_sha256(body)
    assert result.protocol_version == SIGNED_COMMAND_VERSION


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/companion/control", {"command": "status"}),
        ("POST", "/api/companion/other", {"command": "status"}),
        ("POST", "/api/companion/control", {"command": "different"}),
    ],
)
def test_verify_signed_command_rejects_tampered_request_parts(method, path, body):
    private_key = _private_key()
    envelope = _signed_envelope(
        private_key,
        method="POST",
        path="/api/companion/control",
        body={"command": "status"},
    )

    with pytest.raises(SignedCommandError) as exc:
        verify_signed_command(
            method=method,
            path=path,
            body=body,
            envelope=envelope,
            public_key_b64=_public_key_b64(private_key),
            now=NOW,
        )

    assert "signature" in str(exc.value).lower()


def test_verify_signed_command_rejects_stale_timestamp():
    private_key = _private_key()
    envelope = _signed_envelope(private_key)

    with pytest.raises(SignedCommandError) as exc:
        verify_signed_command(
            method="POST",
            path="/api/companion/control",
            body={},
            envelope=envelope,
            public_key_b64=_public_key_b64(private_key),
            now=datetime(2026, 6, 8, 12, 10, 1, tzinfo=timezone.utc),
        )

    assert "freshness" in str(exc.value)


def test_verify_signed_command_rejects_replayed_nonce():
    private_key = _private_key()
    envelope = _signed_envelope(private_key)
    seen = {("phone-1", "nonce-1")}

    def remember_nonce(key_id, nonce, expires_at):
        return (key_id, nonce) not in seen

    with pytest.raises(SignedCommandError) as exc:
        verify_signed_command(
            method="POST",
            path="/api/companion/control",
            body={},
            envelope=envelope,
            public_key_b64=_public_key_b64(private_key),
            remember_nonce=remember_nonce,
            now=NOW,
        )

    assert "nonce" in str(exc.value)


def test_verify_signed_command_rejects_timestamp_without_timezone():
    private_key = _private_key()
    envelope = _signed_envelope(private_key)
    envelope = SignedCommandEnvelope(
        key_id=envelope.key_id,
        timestamp="2026-06-08T12:00:00",
        nonce=envelope.nonce,
        signature=envelope.signature,
    )

    with pytest.raises(SignedCommandError) as exc:
        verify_signed_command(
            method="POST",
            path="/api/companion/control",
            body={},
            envelope=envelope,
            public_key_b64=_public_key_b64(private_key),
            now=NOW,
        )

    assert "timezone" in str(exc.value)


def test_envelope_from_headers_defaults_version_and_is_case_insensitive():
    headers = {
        SIGNED_COMMAND_HEADERS["key_id"].lower(): "phone-1",
        SIGNED_COMMAND_HEADERS["timestamp"].lower(): TIMESTAMP,
        SIGNED_COMMAND_HEADERS["nonce"].lower(): "nonce-1",
        SIGNED_COMMAND_HEADERS["signature"].lower(): "sig",
    }

    envelope = envelope_from_headers(headers)

    assert envelope.version == SIGNED_COMMAND_VERSION
    assert envelope.key_id == "phone-1"
    assert envelope.timestamp == TIMESTAMP
    assert envelope.nonce == "nonce-1"
    assert envelope.signature == "sig"


def test_signing_payload_rejects_unsupported_version():
    envelope = SignedCommandEnvelope(
        version=SIGNED_COMMAND_VERSION + 1,
        key_id="phone-1",
        timestamp=TIMESTAMP,
        nonce="nonce-1",
        signature="sig",
    )

    with pytest.raises(SignedCommandError) as exc:
        signing_payload("POST", "/api/companion/control", {}, envelope)

    assert "version" in str(exc.value)


def test_max_clock_skew_constant_documents_five_minute_window():
    assert MAX_CLOCK_SKEW_SECONDS == 300
