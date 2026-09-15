from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


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
__all__ = ["ChunkDraft"]
