"""入库：提交异步任务并返回受理凭据。"""

from fastapi import APIRouter, status

from ...schemas.ingest import IngestAccepted, IngestSubmit
from ...services.ingestion import INGEST_KIND
from ..deps import AdmissionDep, TaskServiceDep

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=IngestAccepted)
async def submit_ingest(
    payload: IngestSubmit, tasks: TaskServiceDep, _: AdmissionDep
) -> IngestAccepted:
    """**202 而非 200**：解析与向量化是长任务，这里只表示"已受理"。

    客户端拿 task_id 轮询 `GET /tasks/{id}` 看进度。
    """
    task = await tasks.submit(
        INGEST_KIND,
        {
            "kb_id": payload.kb_id,
            "source": payload.source,
            "metadata": payload.metadata,
        },
        idempotency_key=payload.idempotency_key,
        max_attempts=payload.max_attempts,
    )
    return IngestAccepted(
        task_id=task.task_id, status=task.status.value, kb_id=payload.kb_id
    )
