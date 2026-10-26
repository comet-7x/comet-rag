from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class MinioCredentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


def credentials(environ: Mapping[str, str]) -> MinioCredentials:
    access = environ.get("COMET_TEST_MINIO_ACCESS_KEY")
    secret = environ.get("COMET_TEST_MINIO_SECRET_KEY")
    if access is None and secret is None:
        return MinioCredentials("minioadmin", "minioadmin")  # local compose defaults
    if not access or not access.strip() or not secret or not secret.strip():
        raise ValueError("MinIO 测试凭据必须同时配置且非空")
    return MinioCredentials(access, secret)


@asynccontextmanager
async def isolated_object(
    client: Any, payload: bytes, *, key: str = "documents/integration.txt"
) -> AsyncIterator[tuple[str, str]]:
    """SDK 类型由可选 extra 提供；只清理本次已创建桶和预先登记的精确 key。"""
    bucket = f"comet-rag-test-{uuid4().hex}"
    async with AsyncExitStack() as cleanup:
        await client.create_bucket(Bucket=bucket)
        cleanup.push_async_callback(client.delete_bucket, Bucket=bucket)
        # PUT 响应丢失也可能已落库，故调用前登记幂等删除；不列举或清理其他 key。
        cleanup.push_async_callback(client.delete_object, Bucket=bucket, Key=key)
        await client.put_object(Bucket=bucket, Key=key, Body=payload)
        yield bucket, key
