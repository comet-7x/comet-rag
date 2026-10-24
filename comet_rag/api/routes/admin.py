"""管理相关路由。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/health")
async def health() -> dict[str, str]:  # noqa: RUF029
    """存活探针。**刻意不查下游** —— 探针查下游会让一次数据库抖动
    把整个服务从负载均衡里摘掉，故障面反而变大。"""
    return {"status": "ok"}


@router.get("/limits")
async def limits(request: Request) -> dict[str, Any]:
    """并发闸门与积压的实时数字（spec S4-1、S4-2）。

    过载时最先想看的就是这几个：`in_flight` 贴着 `limit` 说明下游是瓶颈，
    `waiting` 一直很高说明该扩容了，`rejected` 在涨说明已经在丢请求。
    没有这个端点，限流是否生效只能靠猜。
    """
    ctx = request.app.state.ctx
    gate = getattr(ctx, "model_gate", None)
    mineru_gate = getattr(ctx, "mineru_gate", None)
    backlog = await ctx.task_service.backlog()
    degradation = getattr(ctx, "degradation", None)
    maintenance = getattr(ctx, "revision_maintenance", None)
    return {
        "accept_new_ingest_tasks": ctx.accept_new_ingest_tasks,
        "model_gate": asdict(gate.stats) if gate is not None else None,
        "mineru_gate": (asdict(mineru_gate.stats) if mineru_gate is not None else None),
        "backlog": backlog,
        "degradation": degradation.stats if degradation is not None else None,
        "revision_reconciliation": (
            await maintenance.status() if maintenance is not None else None
        ),
    }
