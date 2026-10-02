from __future__ import annotations

import pytest

from tests.integration import conftest as integration


@pytest.mark.parametrize(
    ("url", "schemes", "default_port", "host", "port"),
    [
        (
            "postgresql+asyncpg://u:p@db.test:15432/testing",
            ("postgresql+asyncpg",),
            5432,
            "db.test",
            15432,
        ),
        ("redis://redis.test:16379/14", ("redis",), 6379, "redis.test", 16379),
        ("redis://[::1]/0", ("redis",), 6379, "::1", 6379),
    ],
)
def test_probe_uses_configured_service_endpoint(
    url: str,
    schemes: tuple[str, ...],
    default_port: int,
    host: str,
    port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        integration, "_require", lambda h, p, n: calls.append((h, p, n))
    )
    integration._require_service(
        url, "test", schemes=schemes, default_port=default_port
    )
    assert calls == [(host, port, "test")]


@pytest.mark.parametrize(
    "url",
    [
        "redis:///0",
        "http://redis.test",
        "redis://redis.test:0",
        "redis://redis.test:invalid",
    ],
)
def test_invalid_endpoint_is_rejected_before_network_probe(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        integration, "_require", lambda h, p, n: calls.append((h, p, n))
    )
    with pytest.raises(ValueError):
        integration._require_service(url, "test", schemes=("redis",), default_port=6379)
    assert calls == []


@pytest.mark.parametrize("required", [False, True])
def test_missing_service_skips_locally_but_fails_when_required(
    required: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(integration, "_port_open", lambda *args: False)
    if required:
        monkeypatch.setenv("COMET_TEST_REQUIRE_SERVICES", "1")
        try:
            with pytest.raises(pytest.fail.Exception):
                integration._require("db.test", 15432, "test")
        except pytest.skip.Exception as exc:
            raise AssertionError("严格模式不能把服务不可用变成 skip") from exc
    else:
        monkeypatch.delenv("COMET_TEST_REQUIRE_SERVICES", raising=False)
        with pytest.raises(pytest.skip.Exception):
            integration._require("db.test", 15432, "test")
