from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence

from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.engines.offload import run_sync
from comet_rag.engines.pipelines import AsyncHookProvider, PipelineConfig
from comet_rag.ports import NormalizedDocument
from comet_rag.services.chunking import ChunkingService, load_document_blocks
from comet_rag.services.indexing import IndexPlan, IndexPlanner
from comet_rag.services.ingestion import DocumentExtraction, IngestRequest, _classify
from comet_rag.services.knowledge_base import KnowledgeBaseService
from comet_rag.services.revision_context import (
    DEFAULT_MAX_REVISION_CONTEXT_BYTES,
    PublicationPhase,
    RevisionTaskStages,
    get_revision_key,
    load_publication,
    load_recovery_draft,
    load_task_handoff,
)
from comet_rag.tasks import LANE_CPU, LANE_IO, Done, Outcome, StagePipeline, TaskContext

REVISION_INGEST_KIND = "revision-ingest"
REVISION_BINDING = "revision_binding"


def validate_revision_request(
    value: object, *, binding: str, kb_ids: Collection[str]
) -> IngestRequest:
    """API 重试与消费者共用配置/KB 校验；不授权存储 IO。"""
    if not isinstance(value, dict) or value.get(REVISION_BINDING) != binding:
        raise ValueError("revision 入库配置绑定不匹配，拒绝跨命名空间/模型/计划续跑")
    request = IngestRequest.model_validate(value)
    if request.kb_id not in kb_ids:
        raise ValueError("知识库不在 revision 入库范围")
    return request


class RevisionIngestRunner:
    """新命名空间生产入库；持久化阶段优先于 from_scratch 或旧的续跑锚点。

    提取/父块准备在 CPU 道，向量/激活/清理在 IO 道。发布后不再加载原文件或调用
    embedding；GC 失败可以重试，但不能撤销已经提交的 active。
    """

    def __init__(
        self,
        *,
        extraction: DocumentExtraction,
        stages: RevisionTaskStages,
        knowledge_base: KnowledgeBaseService,
        config: PipelineConfig,
        hooks: AsyncHookProvider,
        binding: str,
        kb_ids: Sequence[str],
        hierarchy: HierarchyBuilder | None = None,
        max_context_bytes: int = DEFAULT_MAX_REVISION_CONTEXT_BYTES,
    ) -> None:
        if not binding or not kb_ids:
            raise ValueError("revision 入库必须绑定配置及 KB 范围")
        self._extraction = extraction
        self._stages = stages
        self._kb = knowledge_base
        self._binding = binding
        self._kb_ids = frozenset(kb_ids)
        self._hierarchy = hierarchy
        self._limit = max_context_bytes
        self._chunking = ChunkingService(config, hooks)
        self._planner = IndexPlanner()
        self._flow = StagePipeline()
        for name, lane, operation in (
            ("extracting", LANE_CPU, self._extract),
            ("preparing", LANE_CPU, self._prepare),
            ("indexing", LANE_IO, self._index),
            ("activating", LANE_IO, self._activate),
            ("cleanup", LANE_IO, self._cleanup),
        ):
            self._flow.stage(name, lane=lane)(self._classified(name, operation))

    @staticmethod
    def _classified(
        name: str, operation: Callable[[TaskContext], Awaitable[Outcome | None]]
    ) -> Callable[[TaskContext], Awaitable[Outcome | None]]:
        async def run(ctx: TaskContext) -> Outcome | None:
            try:
                return await operation(ctx)
            except Exception as exc:
                classified = _classify(exc, name)
                if classified is exc:
                    raise
                raise classified from exc

        return run

    def lane_of(self, stage: str | None) -> str | None:
        """生产补投按持久化阶段选择道次，不依赖补投者的进程。"""
        return self._flow.lane_of(stage)

    async def __call__(self, ctx: TaskContext) -> Outcome:
        task = await ctx.snapshot()
        if task.kind != REVISION_INGEST_KIND or not isinstance(task.request, dict):
            raise ValueError("revision 入库任务种类或请求错误")
        request = validate_revision_request(
            task.request, binding=self._binding, kb_ids=self._kb_ids
        )
        draft = load_recovery_draft(task.context, ctx.task_id, max_bytes=self._limit)
        if draft is not None and draft.key.kb_id != request.kb_id:
            raise ValueError("恢复载荷与请求知识库不一致")
        await ctx.checkpoint()
        return await self._flow(ctx)

    async def _extract(self, ctx: TaskContext) -> None:
        context = (await ctx.snapshot()).context
        if any(
            name in context
            for name in ("revision_prepare", "revision_index", "revision_publication")
        ):
            return
        await self._extraction.extract(ctx)

    async def _prepare(self, ctx: TaskContext) -> None:
        task = await ctx.snapshot()
        if "revision_index" in task.context:
            await self._stages.prepare(ctx)
            return
        request = IngestRequest.model_validate(task.request)
        await self._kb.resolve_for_ingest(request.kb_id)
        source_id = task.context["source_id"]
        source = task.context["source"]
        file_type = task.context["file_type"]
        if not all(isinstance(value, str) for value in (source_id, source, file_type)):
            raise TypeError("提取来源必须为字符串")
        key = await get_revision_key(
            ctx, kb_id=request.kb_id, source_id=source_id, max_bytes=self._limit
        )
        document = NormalizedDocument(
            markdown=task.context["text"],
            metadata=task.context.get("document_metadata", {}),
            blocks=load_document_blocks(task.context.get("document_blocks", [])),
        )

        def plan() -> IndexPlan:
            chunks = (
                self._hierarchy.split(document)
                if self._hierarchy is not None
                else self._chunking.split(file_type, document)
            )
            return self._planner.plan(
                chunks,
                key=key,
                source=source,
                file_type=file_type,
                document_metadata=document.metadata,
                request_metadata=request.metadata,
            )

        prepared = await run_sync(plan)
        await self._stages.prepare(ctx, prepared)

    async def _index(self, ctx: TaskContext) -> None:
        task = await ctx.snapshot()
        if "revision_publication" in task.context:
            handoff = load_task_handoff(task.context, ctx.task_id)
            load_publication(task.context, handoff.draft)
            return
        request = IngestRequest.model_validate(task.request)
        kb = await self._kb.resolve_for_ingest(request.kb_id)
        await self._stages.stage(ctx, embedding_dim=kb.embedding_dim)

    async def _activate(self, ctx: TaskContext) -> None:
        await self._stages.activate(ctx)

    async def _cleanup(self, ctx: TaskContext) -> Done:
        await self._stages.cleanup(ctx)
        task = await ctx.snapshot()
        draft = load_task_handoff(task.context, ctx.task_id).draft
        publication = load_publication(task.context, draft)
        if publication is None:
            raise ValueError("发布结果缺失")
        return Done(
            result={
                "kb_id": draft.key.kb_id,
                "source_id": draft.key.source_id,
                "file_type": draft.file_type,
                "chunk_count": draft.child_count,
                "revision_id": draft.key.revision_id,
                "superseded": publication.phase is not PublicationPhase.ACTIVE,
            }
        )
