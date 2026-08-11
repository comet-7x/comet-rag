"""向量存储抽象。

**接口设计的两条纪律**（spec A9 / §7 Never）：

1. `filter` 必须是**结构化 dict**，不得接收后端专有的表达式字符串。
   写成 `filter="kb_id == 'abc'"` 会把接口当场绑死在 Milvus 上，
   换 Qdrant/Weaviate 时要改的是每一个调用点，而不是一个适配器。

2. 后端差异必须**封在实现内部**。Milvus 要显式建 collection、写入后
   需 flush 才可见、过滤用 boolean 表达式；内存版三样都不需要。
   这些差异一旦渗出接口，"可替换"就只是句空话。
   `aensure_collection()` 存在的唯一理由就是吸收第一条差异。

`kb_id` 贯穿所有方法：知识库既是租户隔离边界（spec A5），
也是 Milvus 的 partition key。不带它的接口日后加不进去 —— 那要重灌数据。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


class VectorStoreError(RuntimeError):
    """向量存储相关错误的基类。"""


class CollectionNotFound(VectorStoreError):
    def __init__(self, kb_id: str) -> None:
        super().__init__(f"知识库 {kb_id!r} 尚未创建，请先调用 aensure_collection()")
        self.kb_id = kb_id


class DimensionMismatch(VectorStoreError):
    """写入向量的维度与知识库声明的不符。

    **必须报错，不能静默写入**（spec A12）：换了 embedding 模型之后，
    新旧向量混在同一个空间里，检索结果会静默劣化 —— 不报错、不崩溃，
    只是质量变差，而且事后无法分辨哪些 chunk 该重算。
    """

    def __init__(self, kb_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"知识库 {kb_id!r} 声明维度 {expected}，收到 {actual}。"
            f"通常意味着换了 embedding 模型 —— 请新建知识库或整库重算。"
        )
        self.kb_id, self.expected, self.actual = kb_id, expected, actual


@dataclass(slots=True)
class VectorRecord:
    """一条待写入的向量。

    `id` 由调用方给定而非存储生成，这样重复入库同一文档是**覆盖**而不是
    追加副本（Pipeline 的 chunk id 是 SHA256(source_id:index)，天然稳定）。
    """

    id: str
    text: str
    embedding: Sequence[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchHit:
    id: str
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


#: 过滤条件。**只支持两种形式**，因为每一种都必须能翻译到所有后端：
#:   {"field": value}       等值
#:   {"field": [v1, v2]}    属于集合
#: 多个键之间是 AND。需要 OR / 范围 / 嵌套时应显式扩展本约定并同步所有实现，
#: 而不是让某个后端偷偷多支持一点 —— 那会让调用方写出只在一种后端上能跑的代码。
Filter = dict[str, Any]


def matches_filter(metadata: dict[str, Any], filter: Filter | None) -> bool:
    """内存实现与契约测试共用的过滤语义参考实现。

    真实后端应把它翻译成各自的原生查询，但**语义必须与此一致** ——
    契约测试比对的就是这个行为。
    """
    if not filter:
        return True
    for key, expected in filter.items():
        actual = metadata.get(key)
        if isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


class BaseVectorStore(ABC):
    """向量存储。所有实现必须通过 `tests/contracts/vector_store.py` 的契约。"""

    @abstractmethod
    async def aensure_collection(self, kb_id: str, *, dim: int) -> None:
        """幂等地准备好一个知识库的存储空间。

        已存在且维度一致 → 无操作；维度不一致 → `DimensionMismatch`。
        Milvus 在这里建 collection 与索引，内存版只是记下维度。
        """

    @abstractmethod
    async def aupsert(self, kb_id: str, records: Sequence[VectorRecord]) -> list[str]:
        """按 id 写入或覆盖，返回写入的 id 列表。

        **返回后必须立即可查**。Milvus 写入后不 flush/load 是查不到的，
        这个差异不体现在签名上，只能靠契约测试拦截（plan R1）。
        """

    @abstractmethod
    async def asearch(
        self,
        kb_id: str,
        query_embedding: Sequence[float],
        *,
        top_k: int = 5,
        filter: Filter | None = None,
    ) -> list[SearchHit]:
        """按余弦相似度检索，返回按 score 降序的命中。

        检索范围**限定在 kb_id 内**，不得跨知识库串数据。
        """

    @abstractmethod
    async def adelete(
        self,
        kb_id: str,
        *,
        ids: Sequence[str] | None = None,
        filter: Filter | None = None,
    ) -> int:
        """删除并返回删除条数。`ids` 与 `filter` 至少给一个。"""

    @abstractmethod
    async def adrop_collection(self, kb_id: str) -> None:
        """删除整个知识库。不存在时静默返回（幂等）。"""

    @abstractmethod
    async def acount(self, kb_id: str, *, filter: Filter | None = None) -> int:
        """统计条数，用于校验入库结果与配额。"""

    async def aclose(self) -> None:
        """释放连接。无资源可释放的实现无需覆写。"""
        return None
