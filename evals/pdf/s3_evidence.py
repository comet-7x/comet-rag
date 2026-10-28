from __future__ import annotations

import asyncio
import base64
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import uuid4

from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from comet_rag.core.concurrency import Gate
from comet_rag.infrastructure.sources.base import finish
from evals.pdf.evidence_bundle import (
    MANIFEST_BYTES,
    EvidenceBundle,
    EvidenceReference,
    manifest_files,
    validate_bundle_bytes,
)
from evals.pdf.structured import StructuredProjection


class S3EvidenceError(RuntimeError):
    """只暴露本地错误码与不确定对象名，不回显SDK异常中的端点或凭据。"""

    def __init__(self, code: str, *, uncertain_key: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain_key = uncertain_key


@dataclass(frozen=True, slots=True)
class S3EvidenceLimits:
    total_seconds: float = 120
    request_seconds: float = 30
    received_bytes: int = 128 * 1024 * 1024

    def __post_init__(self) -> None:
        for value in (self.total_seconds, self.request_seconds):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("S3时间预算必须是有限正数")
        if type(self.received_bytes) is not int or self.received_bytes <= 0:
            raise ValueError("S3接收预算必须为正整数")


@dataclass(slots=True)
class _Budget:
    limits: S3EvidenceLimits
    end: float
    clock: Callable[[], float]
    received: int = 0
    last_put: str | None = None

    def remaining(self) -> float:
        value = self.end - self.clock()
        if value <= 0:
            raise S3EvidenceError("deadline_exceeded")
        return value

    def consume(self, size: int) -> None:
        self.received += size
        if self.received > self.limits.received_bytes:
            raise S3EvidenceError("cumulative_receive_limit")


class AsyncS3EvidenceStore:
    """独立证据实验适配；借用原生异步SDK及进程级Gate，不接生产Store Port。"""

    def __init__(
        self,
        *,
        client: Any,  # aioboto3服务客户端及StreamingBody类型由SDK动态生成。
        bucket: str,
        gate: Gate,
        limits: S3EvidenceLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if client.meta.config.retries.get("total_max_attempts") != 1:
            raise ValueError("证据客户端必须关闭SDK自动重试")
        self._client = client
        self._bucket = bucket
        self._gate = gate
        self._limits = limits or S3EvidenceLimits()
        self._clock = clock
        self._verified = False
        # 在任何PUT前公开本次探测的精确key，失败或取消后由所有者清理测试作用域。
        self.probe_key = "evidence-capability/" + uuid4().hex

    def _budget(self) -> _Budget:
        return _Budget(
            self._limits, self._clock() + self._limits.total_seconds, self._clock
        )

    async def _get(
        self,
        key: str,
        expected_sha256: str,
        budget: _Budget,
        *,
        limit: int,
        expected_size: int | None = None,
    ) -> bytes:
        body = None
        error: BaseException | None = None
        try:
            async with asyncio.timeout(
                min(self._limits.request_seconds, budget.remaining())
            ):
                response = await self._client.get_object(
                    Bucket=self._bucket, Key=key, ChecksumMode="ENABLED"
                )
                body = response["Body"]
                declared = response.get("ContentLength")
                if response["ResponseMetadata"]["HTTPStatusCode"] != 200:
                    raise S3EvidenceError("unexpected_get_status")
                if (
                    type(declared) is not int
                    or not 0 < declared <= limit
                    or expected_size is not None
                    and declared != expected_size
                    or response.get("ContentEncoding", "identity") != "identity"
                    or response.get("ContentType", "").split(";", 1)[0]
                    != "application/json"
                ):
                    raise S3EvidenceError("unexpected_object_metadata")
                chunks: list[bytes] = []
                size = 0
                digest = sha256()
                while True:
                    chunk = await body.read(64 * 1024)
                    budget.remaining()
                    budget.consume(len(chunk))
                    size += len(chunk)
                    if size > declared:
                        raise S3EvidenceError("actual_receive_limit")
                    if not chunk:
                        break
                    digest.update(chunk)
                    chunks.append(chunk)
                if size != declared or digest.hexdigest() != expected_sha256:
                    raise S3EvidenceError("object_digest_or_length_mismatch")
                return b"".join(chunks)
        except (TimeoutError, ConnectTimeoutError, ReadTimeoutError):
            error = S3EvidenceError("request_timeout")
            raise error from None
        except BaseException as exc:
            error = exc
            raise
        finally:
            # 原生StreamingBody.close是同步释放连接，不读取剩余体、不关闭共享client。
            if body is not None:
                try:
                    body.close()
                except Exception:
                    if error is None:
                        raise S3EvidenceError("body_close_failed") from None
                    error.add_note("S3响应体关闭失败，连接回收未确认")

    async def _put(
        self, key: str, data: bytes, budget: _Budget, *, expected_conflict: bool = False
    ) -> None:
        try:
            async with asyncio.timeout(
                min(self._limits.request_seconds, budget.remaining())
            ):
                budget.last_put = key
                response = await self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=data,
                    ContentLength=len(data),
                    ContentType="application/json",
                    IfNoneMatch="*",
                    ChecksumSHA256=base64.b64encode(sha256(data).digest()).decode(),
                )
                if expected_conflict:
                    raise S3EvidenceError("conditional_overwrite_accepted")
                if response["ResponseMetadata"]["HTTPStatusCode"] != 200:
                    raise S3EvidenceError("unexpected_put_status", uncertain_key=key)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 412:
                raise S3EvidenceError(
                    "conditional_put_rejected", uncertain_key=key
                ) from None
        except S3EvidenceError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            # 请求可能已提交；不自动再PUT，也不删除可能已成功的证据对象。
            raise S3EvidenceError("put_response_unknown", uncertain_key=key) from None
        if expected_conflict:
            return
        await self._get(
            key,
            sha256(data).hexdigest(),
            budget,
            limit=len(data),
            expected_size=len(data),
        )

    async def verify_target(self) -> None:
        """只能在所有者的新测试桶调用；探测key留给所有者精确清理，不碰业务对象。"""
        self._verified = False
        budget = self._budget()
        try:
            async with asyncio.timeout(budget.remaining()), self._gate:
                initial = b'{"capability":"initial"}'
                await self._put(self.probe_key, initial, budget)
                await self._put(
                    self.probe_key,
                    b'{"capability":"overwrite"}',
                    budget,
                    expected_conflict=True,
                )
                await self._get(
                    self.probe_key,
                    sha256(initial).hexdigest(),
                    budget,
                    limit=len(initial),
                    expected_size=len(initial),
                )
                budget.remaining()
                self._verified = True
        except asyncio.CancelledError:
            raise
        except S3EvidenceError:
            raise
        except Exception:
            raise S3EvidenceError("capability_probe_failed") from None

    async def _read(
        self, reference: EvidenceReference, manifest: bytes, budget: _Budget
    ) -> StructuredProjection:
        facts = manifest_files(manifest, reference)
        payloads = {}
        for name, fact in facts.items():
            payloads[name] = await self._get(
                reference.prefix + "/" + name,
                fact.sha256,
                budget,
                limit=fact.size,
                expected_size=fact.size,
            )
        result = await finish(
            asyncio.create_task(
                asyncio.to_thread(validate_bundle_bytes, manifest, payloads, reference)
            )
        )
        budget.remaining()
        return result

    async def read(self, reference: EvidenceReference) -> StructuredProjection:
        """只读不要求写能力；统一预算从排队前开始，所有对象均按manifest绑定。"""
        budget = self._budget()
        try:
            async with asyncio.timeout(budget.remaining()), self._gate:
                return await self._read(
                    reference,
                    await self._get(
                        reference.prefix + "/manifest.json",
                        reference.manifest_sha256,
                        budget,
                        limit=MANIFEST_BYTES,
                    ),
                    budget,
                )
        except asyncio.CancelledError:
            raise
        except S3EvidenceError:
            raise
        except TimeoutError:
            raise S3EvidenceError("deadline_exceeded") from None
        except Exception:
            raise S3EvidenceError("bundle_read_failed") from None

    async def publish(self, bundle: EvidenceBundle) -> StructuredProjection:
        if not self._verified:
            raise S3EvidenceError("conditional_capability_unverified")
        budget = self._budget()
        try:
            async with asyncio.timeout(budget.remaining()), self._gate:
                payloads = {
                    "structured_content.json": bundle.native,
                    "ledger.json": bundle.ledger,
                }
                await finish(
                    asyncio.create_task(
                        asyncio.to_thread(
                            validate_bundle_bytes,
                            bundle.manifest,
                            payloads,
                            bundle.reference,
                        )
                    )
                )
                budget.remaining()
                for name, data in payloads.items():
                    await self._put(bundle.reference.prefix + "/" + name, data, budget)
                await self._put(
                    bundle.reference.prefix + "/manifest.json", bundle.manifest, budget
                )
                return await self._read(bundle.reference, bundle.manifest, budget)
        except asyncio.CancelledError:
            raise
        except S3EvidenceError:
            raise
        except TimeoutError:
            raise S3EvidenceError(
                "deadline_exceeded", uncertain_key=budget.last_put
            ) from None
        except Exception:
            raise S3EvidenceError(
                "bundle_publish_failed", uncertain_key=budget.last_put
            ) from None
