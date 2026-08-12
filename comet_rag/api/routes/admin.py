"""管理相关路由。"""

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
    backlog = await ctx.task_service.backlog()
    return {
        "model_gate": asdict(gate.stats) if gate is not None else None,
        "backlog": backlog,
    }
