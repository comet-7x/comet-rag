"""入库：提交异步任务并返回受理凭据。"""

from __future__ import annotations

from fastapi import APIRouter, status

from ...engines.offload import run_sync
from ...services.revision_ingestion import REVISION_BINDING
from ..deps import AdmissionDep, ContextDep, SourcePolicyDep, TaskServiceDep
from ..schemas.ingest import IngestAccepted, IngestSubmit

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=IngestAccepted)
async def submit_ingest(
    payload: IngestSubmit,
    tasks: TaskServiceDep,
    policy: SourcePolicyDep,
    _: AdmissionDep,
    ctx: ContextDep,
) -> IngestAccepted:
    """**202 而非 200**：解析与向量化是长任务，这里只表示"已受理"。

    客户端拿 task_id 轮询 `GET /tasks/{id}` 看进度。
    """
    # **在建任务之前**校验来源：建完再拒等于白建一条，还会污染积压统计；
    # 更要紧的是那时 worker 可能已经把它捞走开始读文件了。
    if (
        ctx.revision_ingest_binding is not None
        and payload.kb_id not in ctx.revision_ingest_kb_ids
    ):
        raise ValueError("知识库不在 revision 入库范围")
    if policy is not None:
        # DNS 与本地路径解析会阻塞；取消时仍须等准入线程结束，且不能继续建任务。
        await run_sync(policy.check, payload.source)

    request: dict[str, object] = {
        "kb_id": payload.kb_id,
        "source": payload.source,
        "metadata": payload.metadata,
    }
    if ctx.revision_ingest_binding is not None:
        request[REVISION_BINDING] = ctx.revision_ingest_binding
    task = await tasks.submit(
        ctx.ingest_kind,
        request,
        idempotency_key=payload.idempotency_key,
        max_attempts=payload.max_attempts,
    )
    return IngestAccepted(
        task_id=task.task_id, status=task.status.value, kb_id=payload.kb_id
    )
