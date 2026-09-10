from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import ParseResult, urlparse


class SourceContent:
    """一个尚未加载的本地路径或 URI。"""

    def __init__(self, source: str | Path) -> None:
        self.source = str(source).strip()

    @cached_property
    def parsed_url(self) -> ParseResult:
        return urlparse(self.source)

    @cached_property
    def is_url(self) -> bool:
        if self.parsed_url.scheme.lower() not in ("http", "https"):
            return False
        return bool(self.parsed_url.netloc)

    @cached_property
    def is_local(self) -> bool:
        if self.is_url:
            return False
        try:
            return Path(self.source).is_file()
        except OSError:
            return False

    @cached_property
    def source_type(self) -> str:
        """内建来源保持稳定标签，外部适配器直接使用可扩展 URI scheme。"""
        if self.is_url:
            return "url"
        if self.is_local:
            return "local"
        return self.parsed_url.scheme.lower() or "unknown"

    @cached_property
    def source_id(self) -> str:
        if self.is_local:
            try:
                value = Path(os.path.abspath(self.source)).as_posix()
            except OSError:
                value = self.source
        else:
            value = self.source
        return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class LoadedResource:
    """Loader 交给提取链路的受管本地文件。"""

    path: Path
    source: SourceContent
    is_temp: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    _release: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def file_type(self) -> str:
        value = self.metadata.get("file_type")
        if isinstance(value, str):
            return value.lower().lstrip(".")
        return self.path.suffix.lower().lstrip(".")

    @property
    def file_size(self) -> int:
        value = self.metadata.get("file_size")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return self.path.stat().st_size

    @property
    def media_type(self) -> str | None:
        value = self.metadata.get("media_type")
        return value if isinstance(value, str) else None

    def cleanup(self) -> None:
        """释放消费端持有的临时文件，并且只通知所有者一次。"""
        release, self._release = self._release, None
        try:
            if self.is_temp:
                self.path.unlink(missing_ok=True)
        finally:
            if release is not None:
                release()


@runtime_checkable
class SourceLoaderPort(Protocol):
    """把来源规范化为受管本地文件的跨层契约。

    具体实现可以提供额外选项，但路由器与 Pipeline 只能依赖这里的公共参数。
    批量调用要求显式传并发预算；具体类可以为直接用户提供有理由的默认值。
    """

    def load(self, source: SourceContent | str) -> LoadedResource: ...

    async def aload(self, source: SourceContent | str) -> LoadedResource: ...

    def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int,
    ) -> list[LoadedResource]: ...

    async def abatch_load(
        self,
        sources: list[SourceContent] | list[str],
        *,
        max_concurrency: int,
    ) -> list[LoadedResource]: ...

    def cleanup(self) -> None: ...

    async def acleanup(self) -> None: ...


__all__ = ["LoadedResource", "SourceContent", "SourceLoaderPort"]
