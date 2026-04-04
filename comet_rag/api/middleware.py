from uuid import uuid4

from fastapi import Header, Request
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.tracing import trace_ctx


class TraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("Trace-ID", str(uuid4()))
        trace_ctx.set(trace_id)
        request.state.trace_id = trace_id
        return await call_next(request)


async def get_trace_id(
    trace_id: str | None = Header(None, alias="Trace-ID"),
) -> str | None:
    """获取当前链路 ID"""
    return trace_id
