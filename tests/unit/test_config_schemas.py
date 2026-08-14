"""配置模型的边界行为。

目前只有一件事：**DSN 里的凭据必须百分号编码**。这是 PR 评审 #7 指出的 ——
自动生成的数据库密码常含 `@` `/` `#` 这类保留字符，直接拼进 URL 会把连接串
切成另一个意思，而报错是"host 不存在"，跟密码八竿子打不着。
"""

from __future__ import annotations

import pytest

from comet_rag.config.schemas import (
    IngestPolicyConfig,
    ModelConfig,
    RedisConfig,
    S3Config,
    SqlDatabaseConfig,
    VectorDatabaseConfig,
)

# ── DSN 编码（PR 评审 #7）──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("password", "must_not_contain"),
    [
        ("p@ssword", "@ssword@"),  # @ 会被当成 userinfo 与 host 的分隔符
        ("pa/ss", "/ss@"),  # / 会被当成路径起点
        ("pa:ss", None),  # : 会被当成用户名与密码的分隔符
        ("pa#ss", None),  # # 会被当成 fragment 起点
        ("pa?ss", None),  # ? 会被当成 query 起点
    ],
)
def test_dsn_percent_encodes_credentials(
    password: str, must_not_contain: str | None
) -> None:
    """**保留字符必须被编码**（PR 评审 #7）。

    直接拼接的话，密码里一个 `@` 就把连接串切成了另一个意思：报错是
    "host 不存在"，而不是"密码有问题" —— 极难联想到根因。
    这类密码在生产里很常见（自动生成的密码常含特殊字符）。
    """
    from urllib.parse import urlsplit

    config = SqlDatabaseConfig(
        host="db.internal",
        port=5432,
        username="user",
        password=password,
        database="comet_rag",
    )
    dsn = config.dsn

    # 关键断言：解析回来之后，host / 库名必须还是原来那个
    parsed = urlsplit(dsn.replace("postgresql+asyncpg://", "//", 1))
    assert parsed.hostname == "db.internal", f"host 被密码里的字符改写了：{dsn}"
    assert parsed.port == 5432
    assert parsed.path == "/comet_rag"
    assert parsed.password is not None
    from urllib.parse import unquote

    assert unquote(parsed.password) == password, "解码后与原密码不一致"


def test_dsn_encodes_username_and_database_too() -> None:
    """用户名与库名同样可能含保留字符（例如带域的用户名 `user@corp`）。"""
    from urllib.parse import unquote, urlsplit

    config = SqlDatabaseConfig(
        host="h",
        port=5432,
        username="user@corp",
        password="x",  # noqa: S106 —— 测试用的占位口令
        database="db name",
    )
    parsed = urlsplit(config.dsn.replace("postgresql+asyncpg://", "//", 1))

    assert parsed.hostname == "h"
    assert unquote(parsed.username or "") == "user@corp"
    assert unquote(parsed.path) == "/db name"


def test_download_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        IngestPolicyConfig(max_download_bytes=0)


def test_s3_object_limit_and_credentials_are_validated() -> None:
    with pytest.raises(ValueError):
        S3Config(max_object_bytes=0)
    with pytest.raises(ValueError, match="configured together"):
        S3Config(access_key_id="access-only")
    with pytest.raises(ValueError, match="between 1 and 65535"):
        S3Config(endpoint_url="http://minio:0")


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "https://access:secret@minio.example.test",
        "https://access@minio.example.test",
    ],
)
def test_s3_endpoint_url_rejects_embedded_credentials(endpoint_url: str) -> None:
    with pytest.raises(ValueError, match="must not contain credentials"):
        S3Config(endpoint_url=endpoint_url)


def test_s3_credentials_are_masked_and_explicitly_unwrapped() -> None:
    config = S3Config(
        endpoint_url="http://localhost:9010",
        access_key_id="minio-access",
        secret_access_key="minio-secret",  # noqa: S106 - test sentinel
        session_token="temporary-token",  # noqa: S106 - test sentinel
    )

    rendered = str(config.model_dump(mode="json"))
    assert "minio-access" not in rendered
    assert "minio-secret" not in rendered
    assert "temporary-token" not in rendered
    assert config.access_key_id_value == "minio-access"
    assert config.secret_access_key_value == "minio-secret"  # noqa: S105
    assert config.session_token_value == "temporary-token"  # noqa: S105


def test_secrets_are_masked_but_explicitly_unwrapped_for_connection_urls() -> None:
    secret = "do-not-leak-this-value"  # noqa: S105 — test sentinel
    database = SqlDatabaseConfig(
        host="db.internal",
        port=5432,
        username="user",
        password=secret,
        database="comet",
    )
    redis = RedisConfig(host="redis.internal", port=6379, password=secret)
    model = ModelConfig(
        base_url="https://model.invalid", model_name="model", api_key=secret
    )
    vector = VectorDatabaseConfig(
        endpoint="https://vector.invalid",
        api_key=secret,
        collection_name="chunks",
    )

    for config in (database, redis, model, vector):
        assert secret not in repr(config)
        assert secret not in str(config.model_dump(mode="json"))

    assert secret in database.dsn
    assert secret in redis.url
    assert model.api_key_value == secret
    assert vector.api_key_value == secret
