from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

# 调用方可提供同名 metadata，但这些值最终必须由系统生成，不能被文档内容伪造。
SYSTEM_CHUNK_METADATA_KEYS = frozenset(
    {
        "chunk_end",
        "chunk_index",
        "chunk_start",
        "document_revision",
        "file_type",
        "kb_id",
        "parent_id",
        "source",
        "source_id",
        "total_chunks",
    }
)

# 这些事实只能由切分/结构分析产生；请求 metadata 可以覆盖普通业务标签，不能在
# Chunker 没有观察到页面或标题时凭空造出结构。
CHUNK_FACT_METADATA_KEYS = frozenset(
    {
        "block_kind",
        "heading_path",
        "page_number",
    }
)


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """纯切分阶段的产物，尚未分配来源相关 ID 或 embedding。

    字符位置始终指向 ``NormalizedDocument.markdown`` 的 Unicode code point offset。
    兼容旧 ChunkHook 时无法可靠恢复位置，因此两个位置都为 ``None``。
    ``metadata`` 只放标题路径、页码等块级事实；文档与请求 metadata 由 Service 合并。
    """

    text: str
    ordinal: int
    start_char: int | None = None
    end_char: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("ChunkDraft.text 不能是空字符串")
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool):
            raise TypeError("ChunkDraft.ordinal 必须是 int")
        if self.ordinal < 0:
            raise ValueError("ChunkDraft.ordinal 必须大于等于 0")
        if (self.start_char is None) != (self.end_char is None):
            raise ValueError("start_char 与 end_char 必须同时提供或同时省略")
        if self.start_char is not None and self.end_char is not None:
            if (
                not isinstance(self.start_char, int)
                or isinstance(self.start_char, bool)
                or not isinstance(self.end_char, int)
                or isinstance(self.end_char, bool)
            ):
                raise TypeError("start_char 与 end_char 必须是 int | None")
            if self.start_char < 0:
                raise ValueError("start_char 必须大于等于 0")
            if self.end_char <= self.start_char:
                raise ValueError("end_char 必须大于 start_char")
        invalid_keys = [
            key for key in self.metadata if not isinstance(key, str) or not key
        ]
        if invalid_keys:
            raise ValueError(f"ChunkDraft.metadata 键必须是非空字符串：{invalid_keys!r}")

        # frozen dataclass 只会阻止字段重绑；复制并包成只读视图，避免调用者随后
        # 修改原 dict 或 draft.metadata，导致已经规划好的索引内容静默漂移。
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata))
        )


def merge_chunk_metadata(
    *,
    document: Mapping[str, object] | None = None,
    request: Mapping[str, object] | None = None,
    chunk: Mapping[str, object] | None = None,
    system: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """按文档 < 请求 < 块级事实 < 系统字段的顺序合并 metadata。

    最后两层不可交换：调用方可以覆盖提取器给出的业务标签，但不能伪造页码、
    标题路径、source_id 或 chunk_index。系统字段由 Service 生成并拥有最终解释权。
    """

    merged: dict[str, object] = {}
    protected = SYSTEM_CHUNK_METADATA_KEYS | CHUNK_FACT_METADATA_KEYS
    for layer in (document, request):
        if layer:
            merged.update(
                (key, value)
                for key, value in layer.items()
                if key not in protected
            )
    if chunk:
        merged.update(
            (key, value)
            for key, value in chunk.items()
            if key not in SYSTEM_CHUNK_METADATA_KEYS
        )
    if system:
        merged.update(system)
    return merged


__all__ = [
    "ChunkDraft",
    "CHUNK_FACT_METADATA_KEYS",
    "SYSTEM_CHUNK_METADATA_KEYS",
    "merge_chunk_metadata",
]
