"""编排取源、解析、分块、向量化与幂等写入。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field

from comet_rag.core.concurrency import Overloaded
from comet_rag.core.logging import logger
from comet_rag.engines.embedding.batch import aembed_documents
from comet_rag.engines.loaders.auto_loader import AutoLoader
from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.pipelines import HookProvider, PipelineConfig, PipelineHooks
from comet_rag.engines.utils import compute_sha256
from comet_rag.infrastructure.vectorstore import BaseVectorStore, VectorRecord
from comet_rag.ports import (
    DocumentResourceLimitExceeded,
    EmbeddingPort,
    RetryableDocumentUpstreamError,
)
from comet_rag.services.knowledge_base import KnowledgeBaseService
from comet_rag.tasks import (
    LANE_CPU,
    LANE_IO,
    Done,
    Outcome,
    RetriableError,
    StagePipeline,
    TaskContext,
    register,
)

INGEST_KIND = "ingest"

#: 这些异常意味着"外部依赖此刻不可用"，重试有意义。
#: 解析失败、维度不符、文件格式不支持等确定性错误**不在此列** ——
#: 重试一万次也还是错的，只会白白消耗重试预算并推迟失败告知。
_TRANSIENT = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
)


class IngestRequest(BaseModel):
    """入库请求。会被原样存进 `task.request`，故必须 JSON 友好。"""

    kb_id: str = Field(..., min_length=1, description="目标知识库")
    source: str = Field(..., min_length=1, description="本地路径或 URL")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="附加到每个 chunk 的额外元数据"
    )


@dataclass(slots=True)
class IngestOutcome:
    """`Done.result` 的形状。供 API 层直接返回。"""

    kb_id: str
    source_id: str
    file_type: str
    chunk_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kb_id": self.kb_id,
            "source_id": self.source_id,
            "file_type": self.file_type,
            "chunk_count": self.chunk_count,
        }


def _classify(exc: Exception, stage: str) -> Exception:
    """把瞬时故障翻译成 `RetriableError`，其余原样抛出。

    HTTP 状态码单独判断：5xx 与 429 是"稍后再来"，4xx 是"你请求得不对"，
    后者重试没有任何意义。
    """
    if isinstance(exc, RetryableDocumentUpstreamError):
        return RetriableError(
            f"{stage} 阶段文档服务暂时不可用：{exc!s}",
            code="document_upstream_transient",
        )
    if isinstance(exc, Overloaded):
        # 自家闸门满了，是最典型的"稍后再来"：退避重试正好让压力回落。
        # 判死就浪费了一次本来能成功的入库。
        return RetriableError(f"{stage} 阶段被并发闸门拒绝：{exc!s}", code="overloaded")
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code >= 500 or code == 429:
            return RetriableError(
                f"{stage} 阶段上游返回 {code}", code=f"upstream_{code}"
            )
        return exc
    if isinstance(exc, _TRANSIENT):
        return RetriableError(
            f"{stage} 阶段网络故障：{exc!s}", code=f"{stage}_transient"
        )
    return exc


class IngestRunner:
    """`kind="ingest"` 的 runner。

    做成持有依赖的可调用对象而非模块级函数：runner 需要 loader、embedding
    模型、向量库三个依赖，而 `Runner` 协议的签名里没有注入它们的位置。
    """

    def __init__(
        self,
        *,
        embedding_model: EmbeddingPort,
        vector_store: BaseVectorStore,
        knowledge_base: KnowledgeBaseService,
        loader: BaseLoader | None = None,
        config: PipelineConfig | None = None,
        hooks: HookProvider | None = None,
        max_extracted_text_bytes_by_type: Mapping[str, int] | None = None,
    ) -> None:
        self._embedding_model = embedding_model
        self._vector_store = vector_store
        self._kb = knowledge_base
        self._loader = loader or AutoLoader.default()
        self._config = config or PipelineConfig()
        self._hooks = hooks or PipelineHooks
        self._max_extracted_text_bytes_by_type = {
            file_type.lower(): limit
            for file_type, limit in (max_extracted_text_bytes_by_type or {}).items()
        }
        invalid_limits = {
            file_type: limit
            for file_type, limit in self._max_extracted_text_bytes_by_type.items()
            if limit <= 0
        }
        if invalid_limits:
            raise ValueError(
                f"max_extracted_text_bytes_by_type 必须全部大于 0：{invalid_limits!r}"
            )
        self._flow = self._build_flow()

    async def __call__(self, ctx: TaskContext) -> Outcome:
        """返回类型是 `Outcome` 而非 `Done`：本 runner 会在 chunking 与
        indexing 之间**移交道次**，那一步返回的是 `Handoff`（见 `_build_flow`）。
        标成 `Done` 是笔误 —— 它把这条流水线最关键的一个返回值排除在了签名之外。"""
        return await self._flow(ctx)

    # ── 阶段 ───────────────────────────────────────────────────────────────

    def _build_flow(self) -> StagePipeline:
        """分道理由见 `LANE_CPU` / `LANE_IO` 的注释。

        切在 chunking 与 indexing 之间，是因为这里正好是**负载性质翻转**的
        地方：前两个阶段是解析与切分（CPU 密集，靠多进程扩容），indexing 是
        调模型 + 写向量库（IO 密集，靠单进程高并发扩容）。

        切口也恰好是**中间态最小**的地方：交接时 context 里只有一串 chunk
        文本；换成在 indexing 中间切，就得把 200×1024 维的向量塞进任务表。

        单进程部署（`InProcessExecutor`）下 lane 全部被忽略，三个阶段照旧
        一口气跑完 —— 分道不是新的执行模型，只是多 worker 时的路由信息。
        """
        flow = StagePipeline()
        flow.stage("extracting", lane=LANE_CPU)(self._extract)
        flow.stage("chunking", lane=LANE_CPU)(self._chunk)
        flow.stage("indexing", lane=LANE_IO)(self._index)
        return flow

    async def _extract(self, ctx: TaskContext) -> None:
        """取源 + 解析 + 清洗 → markdown 文本。"""
        task = await ctx.snapshot()
        request = IngestRequest.model_validate(task.request)

        loader_content: LoaderContent | None = None
        try:
            # 下载也属于 extracting 阶段，必须位于同一个异常分类边界内。
            # 否则连接超时会绕过 _classify，第一次失败就把任务判死。
            loader_content = await self._loader.aload(SourceContent(request.source))
            file_type = str(loader_content.metadata.get("file_type", "")).lower()
            text = await self._hooks.aextract(file_type, loader_content, self._config)
            self._validate_extracted_text_size(text, file_type)
            extracted_text_bytes = len(text.encode("utf-8"))

            await ctx.put(
                text=text,
                # 原文会在 chunking 后清掉；保留这个小标量才能在终态任务上
                # 观察外部提取量，而不把整份 Markdown 长期留在任务表。
                extracted_text_bytes=extracted_text_bytes,
                file_type=file_type,
                source_id=loader_content.source.source_id,
                source=loader_content.source.source,
                file_name=loader_content.metadata.get("file_name"),
            )
            await ctx.report(message=f"已提取 {len(text)} 字符")
        except Exception as exc:
            raise _classify(exc, "extracting") from exc
        finally:
            # 临时文件必须清掉，否则批量入库会把磁盘塞满
            if loader_content is not None:
                loader_content.cleanup()

    def _validate_extracted_text_size(self, text: str, file_type: str) -> None:
        """在跨 worker 持久化前复验；不能只信任某个 provider 的入口检查。"""
        limit = self._max_extracted_text_bytes_by_type.get(file_type)
        if limit is None:
            return
        size = len(text.encode("utf-8"))
        if size > limit:
            raise DocumentResourceLimitExceeded(
                f"{file_type} 提取文本超过 Task context 限制：{size} > {limit} bytes"
            )

    async def _chunk(self, ctx: TaskContext) -> None:
        task = await ctx.snapshot()
        text: str = task.context["text"]
        file_type: str = task.context["file_type"]

        chunker = self._hooks.get_chunker(file_type)
        chunks = await asyncio.to_thread(chunker, text, self._config)

        # 清掉原始文本：留着的话 context 会同时装文本和 chunk，体积翻倍，
        # 而后续阶段再也用不到它。
        await ctx.put(chunks=chunks, text=None)
        await ctx.report(message=f"已切分 {len(chunks)} 块")

    async def _drop_stale_tail(
        self, kb_id: str, source_id: str, new_count: int, previous: int
    ) -> None:
        """删掉旧版本比新版本多出来的那些块。

        chunk id 是 `sha256(f"{source_id}:{index}")` —— **确定性、按序号**。
        所以重新入库是**原地覆盖** 0..N-1，唯一会变成幽灵的只有序号 ≥ N 的尾巴。
        """
        if previous <= new_count:
            return
        stale = [compute_sha256(f"{source_id}:{i}") for i in range(new_count, previous)]
        await self._vector_store.adelete(kb_id, ids=stale)
        logger.info(f"清理旧版本多余的 {len(stale)} 块 source_id={source_id[:12]}")

    async def _index(self, ctx: TaskContext) -> Done:
        """向量化并写入。按窗口边算边写，内存占用与文档大小无关。"""
        task = await ctx.snapshot()
        request = IngestRequest.model_validate(task.request)
        chunks: list[str] = task.context["chunks"]
        source_id: str = task.context["source_id"]
        file_type: str = task.context["file_type"]

        # 入库前的一致性检查（spec A12 在写路径上的执行点）：
        # 库必须存在，且建库时的 embedding 模型必须与当前配置一致。
        # 维度不符会被向量库拦下，但**同维度的不同模型**谁也拦不住 ——
        # 混用不报错、只是检索静默劣化，事后还分不清哪些 chunk 该重算。
        # 这两类都是确定性错误，刻意放在 _classify 之外让它们一次判死。
        kb = await self._kb.resolve_for_ingest(request.kb_id)

        try:
            await self._vector_store.aensure_collection(
                request.kb_id, dim=kb.embedding_dim
            )

            # 旧版本有多少块 —— 用来算写完之后该清掉哪些尾巴。
            # **必须在写之前数**：写完之后新旧混在一起就分不出来了。
            previous = await self._vector_store.acount(
                request.kb_id, filter={"source_id": source_id}
            )

            base_metadata = {
                **request.metadata,
                "kb_id": request.kb_id,  # spec A5 的租户维度
                "source": task.context.get("source"),
                "source_id": source_id,
                "file_type": file_type,
                "total_chunks": len(chunks),
            }

            window = self._config.embed_batch_size
            written = 0
            for start in range(0, len(chunks), window):
                await ctx.checkpoint()
                batch = chunks[start : start + window]
                embeddings = await aembed_documents(
                    self._embedding_model,
                    batch,
                    max_concurrency=self._config.max_concurrency,
                )
                records = [
                    VectorRecord(
                        id=compute_sha256(f"{source_id}:{start + offset}"),
                        text=text,
                        embedding=embedding,
                        metadata={**base_metadata, "chunk_index": start + offset},
                    )
                    for offset, (text, embedding) in enumerate(
                        zip(batch, embeddings, strict=True)
                    )
                ]
                await self._vector_store.aupsert(request.kb_id, records)
                written += len(records)
                await ctx.report(
                    progress=0.66 + 0.34 * written / max(len(chunks), 1),
                    message=f"已写入 {written}/{len(chunks)} 块",
                )

            # 新版本比旧的短时，清掉多出来的尾巴。
            # 必须先完成全部写入，失败时才能保留仍可用的旧版本。
            await self._drop_stale_tail(request.kb_id, source_id, len(chunks), previous)
        except Exception as exc:
            raise _classify(exc, "indexing") from exc

        logger.info(
            f"入库完成 kb={request.kb_id} source_id={source_id[:12]} chunks={written}"
        )
        # chunks 已经落进向量库，没必要继续占着任务表
        await ctx.put(chunks=None)
        return Done(
            result=IngestOutcome(
                kb_id=request.kb_id,
                source_id=source_id,
                file_type=file_type,
                chunk_count=written,
            ).to_dict(),
            message=f"已入库 {written} 块",
        )


def register_ingest_runner(runner: IngestRunner) -> None:
    """把 runner 绑到 `kind="ingest"`。

    `replace=True`：这是装配代码，应用每次启动都会调一次，必须幂等。
    """
    register(INGEST_KIND, replace=True)(runner)


__all__ = [
    "INGEST_KIND",
    "IngestOutcome",
    "IngestRequest",
    "IngestRunner",
    "register_ingest_runner",
]
