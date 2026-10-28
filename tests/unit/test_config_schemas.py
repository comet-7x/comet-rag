"""配置模型的边界行为。

目前只有一件事：**DSN 里的凭据必须百分号编码**。这是 PR 评审 #7 指出的 ——
自动生成的数据库密码常含 `@` `/` `#` 这类保留字符，直接拼进 URL 会把连接串
切成另一个意思，而报错是"host 不存在"，跟密码八竿子打不着。
"""

from __future__ import annotations

from typing import Any

import pytest

from comet_rag.config.schemas import (
    APPConfig,
    Backend,
    BackendsConfig,
    IngestPolicyConfig,
    LimitsConfig,
    MinerUConfig,
    ModelConfig,
    RedisConfig,
    RetrievalConfig,
    S3Config,
    SqlDatabaseConfig,
    VectorDatabaseConfig,
)
from comet_rag.engines.defaults import (
    DEFAULT_EMBED_FANOUT,
    DEFAULT_EMBED_WINDOW,
    DEFAULT_LOADER_CONCURRENCY,
)
from comet_rag.engines.pipelines.types import PipelineConfig
from comet_rag.infrastructure.extractors.mineru import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MAX_MARKDOWN_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_PARSE_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_UPLOAD_TIMEOUT_SECONDS,
)


def test_revision_read_defaults_are_disabled_and_bounded() -> None:
    settings = RetrievalConfig()
    assert not settings.revision_aware
    assert settings.max_revision_candidates == 2048


@pytest.mark.parametrize(
    "name,value",
    [
        ("degrade_failure_rate", (-0.2, 0.5, 0.8)),
        ("degrade_failure_rate", (0, 0.5, 0.8)),
        ("degrade_failure_rate", (0.2, 0.5, 1.1)),
        ("degrade_failure_rate", (float("nan"), 0.5, 0.8)),
        ("degrade_saturation", (2, -5, 10)),
        ("degrade_saturation", (2, 5, float("inf"))),
        ("degrade_saturation", (10, 5, 2)),
    ],
)
def test_degradation_thresholds_fail_config_preflight(name, value):
    with pytest.raises(ValueError):
        LimitsConfig(**{name: value})


def test_ingest_metadata_budget_applies_to_api_and_service():
    from comet_rag.api.schemas.ingest import IngestSubmit  # noqa: PLC0415
    from comet_rag.services.ingestion import IngestRequest  # noqa: PLC0415

    for model in (IngestSubmit, IngestRequest):
        assert model(kb_id="kb", source="source", metadata={"key": "ok"}).metadata == {
            "key": "ok"
        }
        for metadata in (
            {"large": "x" * 65536},
            {"nonfinite": float("nan")},
            {"invalid": object()},
        ):
            with pytest.raises(ValueError):
                model(kb_id="kb", source="source", metadata=metadata)


@pytest.mark.parametrize("cap", [0, -1, 2049, True, 1.5, "64"])
def test_revision_candidate_budget_is_a_strict_bounded_integer(cap) -> None:
    with pytest.raises(ValueError):
        RetrievalConfig(max_revision_candidates=cap)


@pytest.mark.parametrize("flag", [1, "true", None])
def test_revision_read_flag_requires_explicit_boolean(flag) -> None:
    with pytest.raises(ValueError):
        RetrievalConfig(revision_aware=flag)


def test_revision_read_settings_reject_typos_and_unimplemented_write_flag() -> None:
    with pytest.raises(ValueError):
        RetrievalConfig.model_validate({"revision_write": True})


@pytest.mark.parametrize(
    ("vectors", "tasks", "database", "vector_database", "error"),
    [
        ("milvus", "memory", True, True, "task_store=postgres"),
        ("milvus", "postgres", False, True, "database settings"),
        ("milvus", "postgres", True, False, "vector_database settings"),
        ("postgres", "memory", False, False, "only supports memory or milvus"),
    ],
)
def test_revision_read_config_rejects_non_durable_or_incomplete_milvus(
    vectors,
    tasks,
    database,
    vector_database,
    error,
) -> None:
    infra = {
        "embedding_model": {
            "base_url": "http://fake/v1",
            "model_name": "fake",
            "dim": 3,
        }
    }
    if database:
        infra["database"] = {
            "host": "fake",
            "port": 5432,
            "username": "comet",
            "password": "test",
            "database": "test",
        }
    if vector_database:
        infra["vector_database"] = {
            "endpoint": "http://fake:19530",
            "database_name": "test",
        }
    with pytest.raises(ValueError, match=error):
        APPConfig.model_validate(
            {
                "server_config": {"host": "127.0.0.1", "port": 8000},
                "infrastructure_config": infra,
                "backends": {"vector_store": vectors, "task_store": tasks},
                "retrieval": {"revision_aware": True},
            }
        )


# ── DSN 编码（PR 评审 #7）──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("task_store", "vector_store", "message"),
    [
        (Backend.MEMORY, Backend.MEMORY, "task_store=postgres"),
        (Backend.MEMORY, Backend.MILVUS, "task_store=postgres"),
        (Backend.POSTGRES, Backend.MEMORY, "vector_store=milvus"),
    ],
)
def test_arq_rejects_process_local_storage(
    task_store: Backend, vector_store: Backend, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        BackendsConfig(
            task_executor=Backend.ARQ,
            task_store=task_store,
            vector_store=vector_store,
        )


def test_arq_accepts_shared_storage() -> None:
    config = BackendsConfig(
        task_executor=Backend.ARQ,
        task_store=Backend.POSTGRES,
        vector_store=Backend.MILVUS,
    )
    assert config.task_executor is Backend.ARQ


@pytest.mark.parametrize("task_store", [Backend.MEMORY, Backend.POSTGRES])
def test_inprocess_keeps_memory_vector_store_available(task_store: Backend) -> None:
    config = BackendsConfig(task_store=task_store)
    assert config.task_executor is Backend.INPROCESS
    assert config.vector_store is Backend.MEMORY


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


def test_mineru_is_disabled_by_default_with_bounded_defaults() -> None:
    config = MinerUConfig()

    assert config.enabled is False
    assert config.connect_timeout_seconds == DEFAULT_CONNECT_TIMEOUT_SECONDS
    assert config.request_timeout_seconds == DEFAULT_REQUEST_TIMEOUT_SECONDS
    assert config.upload_timeout_seconds == DEFAULT_UPLOAD_TIMEOUT_SECONDS
    assert config.poll_interval_seconds == 1.0
    assert config.parse_timeout_seconds == DEFAULT_PARSE_TIMEOUT_SECONDS
    assert config.max_response_bytes == DEFAULT_MAX_RESPONSE_BYTES
    assert config.max_markdown_bytes == DEFAULT_MAX_MARKDOWN_BYTES


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8989",
        "http://127.0.0.1:8989",
        "http://127.42.0.1:8989",
        "http://[::1]:8989",
        "https://mineru.internal/api",
    ],
)
def test_mineru_endpoint_accepts_loopback_http_and_remote_https(
    base_url: str,
) -> None:
    assert MinerUConfig(base_url=base_url).base_url == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "http://mineru.internal:8989",
        "http://10.0.0.8:8989",
        "http://0.0.0.0:8989",
    ],
)
def test_mineru_endpoint_rejects_remote_cleartext_http(base_url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        MinerUConfig(base_url=base_url)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "mineru.internal"},
        {"base_url": "http://user:secret@mineru.internal"},
        {"poll_interval_seconds": 10.0, "parse_timeout_seconds": 10.0},
        {"request_timeout_seconds": 901.0},
        {"upload_timeout_seconds": 901.0},
        {"connect_timeout_seconds": 901.0},
        {"max_response_bytes": 10, "max_markdown_bytes": 11},
    ],
)
def test_mineru_cross_field_and_endpoint_constraints(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        MinerUConfig(**kwargs)


def test_mineru_rejects_unmanaged_request_headers() -> None:
    secret = "must-not-enter-config-output"  # noqa: S105 - test sentinel

    with pytest.raises(ValueError, match="headers"):
        MinerUConfig(headers={"Authorization": f"Bearer {secret}"})  # type: ignore[call-arg]


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
        database_name="comet_rag",
    )

    for config in (database, redis, model, vector):
        assert secret not in repr(config)
        assert secret not in str(config.model_dump(mode="json"))

    assert secret in database.dsn
    assert secret in redis.url
    assert model.api_key_value == secret
    assert vector.api_key_value == secret


def test_vector_database_requires_an_explicit_safe_database_and_prefix() -> None:
    with pytest.raises(ValueError, match="database_name"):
        VectorDatabaseConfig(
            endpoint="http://milvus.invalid:19530",
            collection_name="legacy-unused-field",  # type: ignore[call-arg]
        )
    with pytest.raises(ValueError, match="database_name"):
        VectorDatabaseConfig(
            endpoint="http://milvus.invalid:19530", database_name="   "
        )
    with pytest.raises(ValueError, match="collection_prefix"):
        VectorDatabaseConfig(
            endpoint="http://milvus.invalid:19530",
            database_name="test",
            collection_prefix="invalid-prefix",
        )
    with pytest.raises(ValueError, match="replica_number"):
        VectorDatabaseConfig(
            endpoint="http://milvus.invalid:19530",
            database_name="test",
            replica_number=0,
        )


# ── 并发默认值：一处定义，不许漂移 ─────────────────────────────────────────


def test_config_defaults_match_the_library_fallbacks() -> None:
    """**部署默认值与"当库用"的兜底必须一致。**

    这两个数字有意地各写了一遍字面量：`config/` 在分层里低于 `engines/`，
    import 过去就是向上依赖（正是 `test_layering.py` 在守的方向）。重复一个
    字面量、用这条测试防漂移，好过为了省一个字面量把依赖方向弄反。

    一致不是巧合而是要求：库用户和服务用户拿到不同的并发行为，是最难查的那类
    "在我机器上是好的"。
    """
    limits = LimitsConfig()

    assert limits.pipeline_concurrency == DEFAULT_EMBED_FANOUT
    assert limits.embed_batch_size == DEFAULT_EMBED_WINDOW
    assert limits.loader_concurrency == DEFAULT_LOADER_CONCURRENCY
    assert limits.task_chunk_context_max_bytes == 32 * 1024 * 1024


def test_pipeline_defaults_come_from_the_same_place() -> None:
    """`PipelineConfig` 自己的默认值也走同一处，而不是再写一遍。"""
    config = PipelineConfig()

    assert config.max_concurrency == DEFAULT_EMBED_FANOUT
    assert config.embed_batch_size == DEFAULT_EMBED_WINDOW


@pytest.mark.parametrize("timeout", [0, -1, True, "1", float("inf"), float("nan")])
def test_query_deadline_requires_finite_positive_seconds(timeout) -> None:
    with pytest.raises(ValueError):
        RetrievalConfig(timeout_seconds=timeout)


def test_query_deadline_default_and_explicit_budget() -> None:
    assert RetrievalConfig().timeout_seconds == 30.0
    assert RetrievalConfig(timeout_seconds=0.25).timeout_seconds == 0.25
