"""URL validation helpers for server-side outbound requests."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import urlparse, urlsplit


class UnsafeURL(ValueError):
    """Raised when a user-supplied URL targets an unsafe network location."""


_TRUE_VALUES = {"1", "true", "yes", "on"}


_INTERNAL_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
}

_INTERNAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".intranet",
)

_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _resolve_hostname_ips(hostname: str) -> list[ipaddress._BaseAddress]:
    ips: list[ipaddress._BaseAddress] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
        if family in (socket.AF_INET, socket.AF_INET6):
            ips.append(ipaddress.ip_address(sockaddr[0]))
    return ips


def _blocked_ip(addr: ipaddress._BaseAddress) -> bool:
    return (
        any(addr in net for net in _BLOCKED_NETWORKS)
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
    )


def _host_resolves_publicly(hostname: str) -> bool:
    host = hostname.strip().lower()
    if host in _INTERNAL_HOSTNAMES or host.endswith(_INTERNAL_SUFFIXES):
        return False
    try:
        return not _blocked_ip(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        addrs = _resolve_hostname_ips(host)
    except OSError:
        return False
    return bool(addrs) and all(not _blocked_ip(addr) for addr in addrs)


def is_public_http_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return _host_resolves_publicly(parsed.hostname)


def validate_public_http_url(url: str, *, max_length: int = 2048) -> str:
    """Validate a user/API-token supplied server-side HTTP(S) endpoint.

    This is for untrusted outbound URLs, not admin-created model endpoints
    that are intentionally allowed to point at private model providers. DNS
    failures fail closed, and DNS checks reduce obvious private-network
    targets but do not eliminate every DNS rebinding race by themselves.
    """
    cleaned = (url or "").strip()
    if len(cleaned) > max_length:
        raise ValueError("URL is too long")
    if not is_public_http_url(cleaned):
        raise ValueError("URL must point to a public HTTP(S) endpoint")
    return cleaned


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def private_integration_urls_allowed() -> bool:
    return env_flag("ODYSSEUS_ALLOW_PRIVATE_INTEGRATION_URLS", False)


def private_model_endpoints_allowed() -> bool:
    return env_flag("ODYSSEUS_ALLOW_PRIVATE_MODEL_ENDPOINTS", False)


def validate_http_url(
    url: str,
    *,
    allow_private: bool = False,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
    max_len: int = 2048,
) -> str:
    candidate = (url or "").strip()
    if not candidate:
        raise UnsafeURL("URL is required")
    if len(candidate) > max_len:
        raise UnsafeURL("URL is too long")
    if any(ch in candidate for ch in "\r\n\t"):
        raise UnsafeURL("URL contains invalid whitespace")

    try:
        parsed = urlsplit(candidate)
    except Exception as exc:
        raise UnsafeURL("Invalid URL") from exc

    if parsed.scheme.lower() not in allowed_schemes:
        raise UnsafeURL("Only HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeURL("Credentials in URLs are not allowed")

    host = parsed.hostname
    if not host:
        raise UnsafeURL("URL must include a hostname")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise UnsafeURL("Invalid URL port") from exc

    if not allow_private:
        _reject_private_host(host)
    return candidate


def _reject_private_host(host: str) -> None:
    normalized = host.strip().strip("[]").rstrip(".").lower()
    if not normalized:
        raise UnsafeURL("URL must include a hostname")
    if normalized in _INTERNAL_HOSTNAMES or normalized.endswith(".localhost"):
        raise UnsafeURL("Localhost URLs are not allowed")

    addresses = _resolve_addresses(normalized)
    if not addresses:
        raise UnsafeURL("URL hostname could not be resolved")
    for address in addresses:
        if not address.is_global:
            raise UnsafeURL("Private, loopback, link-local, or reserved URLs are not allowed")


def _resolve_addresses(host: str) -> list[ipaddress._BaseAddress]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    resolved: list[ipaddress._BaseAddress] = []
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURL("URL hostname could not be resolved") from exc
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            continue
        if ip not in resolved:
            resolved.append(ip)
    return resolved


def validate_tcp_port(value: str | int | None, *, default: str = "") -> str:
    if value in (None, ""):
        return default
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,5}", text):
        raise ValueError("Invalid port")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    return str(port)


def validate_ssh_destination(host: str | None) -> str:
    text = (host or "").strip()
    if not text:
        return ""
    if len(text) > 255 or text.startswith("-"):
        raise ValueError("Invalid SSH host")
    if any(ord(ch) < 32 or ch.isspace() for ch in text):
        raise ValueError("Invalid SSH host")
    if not re.fullmatch(r"[A-Za-z0-9_.@:%+\[\]-]+", text):
        raise ValueError("Invalid SSH host")
    return text
