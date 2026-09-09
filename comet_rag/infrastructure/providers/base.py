"""需要共享进程级闸门的外部服务适配器基类。"""

from __future__ import annotations

from comet_rag.ports.gate import GatedResource


class GatedModel(GatedResource):
    """接上进程级闸门的模型适配器。行为与 `GatedResource` 完全一致。"""


__all__ = ["GatedModel"]
