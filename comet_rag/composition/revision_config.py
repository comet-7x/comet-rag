from __future__ import annotations

import json
from hashlib import sha256
from urllib.parse import urlsplit

from comet_rag.config.schemas import APPConfig
from comet_rag.engines.pipelines import PipelineConfig


def revision_binding(config: APPConfig, pipeline: PipelineConfig) -> str | None:
    """跨 API/worker 的配置身份；不保存凭据，也不是旧命名空间已排干的证明。"""
    writing = config.revision_ingestion
    if not writing.enabled:
        return None
    if (
        writing.parent_chunk_size is not None
        and writing.parent_chunk_size < pipeline.chunk_size
    ):
        raise ValueError("parent_chunk_size 不能小于子块 chunk_size")
    infrastructure = config.infrastructure_config
    sql, vector, redis = (
        infrastructure.database,
        infrastructure.vector_database,
        infrastructure.redis,
    )
    if sql is None or vector is None or redis is None:
        raise ValueError("revision 入库缺少配套存储配置")

    def endpoint(value: str) -> tuple[str, str | None, int | None, str]:
        parsed = urlsplit(value if "://" in value else "http://" + value)
        return parsed.scheme, parsed.hostname, parsed.port, parsed.path

    model = infrastructure.embedding_model
    # 自定义模型对象/函数无法在新进程中稳定重建；启用写入时拒绝不可序列化的计划设置。
    payload = {
        "version": 1,
        "sql": (sql.host, sql.port, sql.database),
        "vector": (
            endpoint(vector.endpoint),
            vector.database_name,
            vector.collection_prefix,
        ),
        "redis": (redis.host, redis.port, redis.db_index),
        "model": (endpoint(model.base_url), model.model_name, model.dim),
        "pipeline": pipeline.model_dump(mode="json"),
        "parent_chunk_size": writing.parent_chunk_size,
        "kb_ids": sorted(writing.kb_ids),
    }
    return sha256(
        json.dumps(
            payload, ensure_ascii=False, allow_nan=False, sort_keys=True
        ).encode()
    ).hexdigest()
