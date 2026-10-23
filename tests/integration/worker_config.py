from __future__ import annotations

from urllib.parse import unquote, urlparse

from comet_rag.config.schemas import (
    APPConfig,
    Backend,
    BackendsConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    IngestPolicyConfig,
    RedisConfig,
    ServerConfig,
    SqlDatabaseConfig,
    VectorDatabaseConfig,
)


def make_worker_config(
    postgres_dsn: str,
    redis_url: str,
    *,
    redis_db: int,
    dim: int = 4,
    milvus_uri: str = "http://unused",
    milvus_database: str = "zhihao_test_database",
    collection_prefix: str = "test_worker_",
) -> APPConfig:
    """生产装配使用探测通过的测试端点；队列固定时另选专用 Redis DB。"""
    return APPConfig(
        server_config=ServerConfig(host="127.0.0.1", port=0),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="stub-embed", dim=dim
            ),
            database=database_config(postgres_dsn),
            redis=redis_config(redis_url, db_index=redis_db),
            vector_database=VectorDatabaseConfig(
                endpoint=milvus_uri,
                database_name=milvus_database,
                collection_prefix=collection_prefix,
            ),
        ),
        backends=BackendsConfig(
            task_store=Backend.POSTGRES,
            vector_store=Backend.MILVUS,
            task_executor=Backend.ARQ,
        ),
        ingest_policy=IngestPolicyConfig(allow_local=True),
    )


def database_config(dsn: str) -> SqlDatabaseConfig:
    database = urlparse(dsn)
    if database.scheme not in {"postgresql", "postgresql+asyncpg"} or database.query:
        raise ValueError("测试配置仅支持无查询参数的 PostgreSQL DSN")
    if not database.hostname:
        raise ValueError("测试端点必须包含主机")
    return SqlDatabaseConfig(
        host=database.hostname,
        port=database.port or 5432,
        username=unquote(database.username or ""),
        password=unquote(database.password or ""),
        database=unquote(database.path.lstrip("/")),
    )


def redis_config(url: str, *, db_index: int) -> RedisConfig:
    redis = urlparse(url)
    # RedisConfig 尚不能表达 TLS/ACL 用户；测试不得悄悄改连明文或默认用户。
    if redis.scheme != "redis" or redis.username or redis.query:
        raise ValueError("测试配置不能表达此 Redis DSN")
    if not redis.hostname:
        raise ValueError("测试端点必须包含主机")
    return RedisConfig(
        host=redis.hostname,
        port=redis.port or 6379,
        password=unquote(redis.password) if redis.password else None,
        db_index=db_index,
    )
