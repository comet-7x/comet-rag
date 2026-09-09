"""组合根：集中选择实现并装配长生命周期资源。"""

from .bootstrap import build_context
from .context import Context, wire_runners

__all__ = ["Context", "build_context", "wire_runners"]
