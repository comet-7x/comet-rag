from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def validate_http_endpoint(value: str, *, name: str) -> str:
    """验证固定上游地址；明文传输只允许真正的本机回环端点。"""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} 不是合法的绝对 HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError(f"{name} 必须是绝对 HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} 不能包含凭据")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{name} 不能包含 query 或 fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{name} 端口必须在 1 到 65535 之间")
    if parsed.scheme == "http" and not _is_loopback(hostname):
        raise ValueError(f"{name} 的非回环端点必须使用 HTTPS")
    return value.rstrip("/")


def _is_loopback(hostname: str) -> bool:
    if hostname.casefold().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


__all__ = ["validate_http_endpoint"]
