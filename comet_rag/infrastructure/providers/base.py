"""模型适配器共用的实现细节。

进程级限流是**所有**会打外部服务的资源共同面对的事，不只是模型 —— loader
同样要。所以实现搬到了 `ports/gate.py`，这里只保留一个按用途命名的别名，
让适配器那边读起来仍然是"模型的闸门"。

契约在 :mod:`comet_rag.ports`；本模块只解决"怎么做"。
"""

from __future__ import annotations

from comet_rag.ports.gate import GatedResource


class GatedModel(GatedResource):
    """接上进程级闸门的模型适配器。行为与 `GatedResource` 完全一致。"""


__all__ = ["GatedModel"]
