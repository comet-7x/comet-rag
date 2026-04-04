from contextvars import ContextVar, Token

_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


class TraceContext:
    """
    链路追踪上下文。

    在异步/多协程场景下，ContextVar 天然隔离不同协程的值，
    但需要通过 set() 返回的 Token 来正确 reset，否则协程间可能串号。

    用法示例（FastAPI middleware）：
        token = trace_ctx.set(new_trace_id)
        try:
            await call_next(request)
        finally:
            trace_ctx.reset(token)  # 确保离开作用域后恢复原值
    """

    @property
    def trace_id(self) -> str | None:
        return _trace_id_var.get()

    def set(self, trace_id: str) -> Token:
        """
        设置当前上下文的 trace_id，返回 Token 以便后续 reset。
        在异步中间件或上下文管理器里务必调用 reset(token) 还原。
        """
        return _trace_id_var.set(trace_id)

    def reset(self, token: Token) -> None:
        """还原到 set() 调用之前的值，防止协程间 trace_id 泄漏。"""
        _trace_id_var.reset(token)

    def clear(self) -> None:
        """将 trace_id 重置为默认值（None），适用于不需要精确还原的场景。"""
        _trace_id_var.set(None)  # type: ignore[arg-type]


trace_ctx = TraceContext()
