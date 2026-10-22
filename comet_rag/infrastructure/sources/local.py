from __future__ import annotations

from pathlib import Path
from typing import Any

from comet_rag.engines.offload import run_sync
from comet_rag.infrastructure.sources.base import AsyncBaseLoader, BaseLoader
from comet_rag.infrastructure.sources.file_info import build_file_metadata
from comet_rag.ports.source import LoadedResource, SourceContent


def _load_local(source: SourceContent | str) -> LoadedResource:
    source = source if isinstance(source, SourceContent) else SourceContent(source)
    if not source.is_local:
        raise ValueError(
            f"LocalLoader only handles local paths, got: {source.source!r}"
        )
    return LoadedResource(
        path=Path(source.source),
        source=source,
        is_temp=False,
        metadata=build_file_metadata(Path(source.source), source),
    )


class LocalLoader(BaseLoader):
    def _load(self, source: SourceContent | str, **kwargs: Any) -> LoadedResource:
        self._reject_unsupported(kwargs)
        return _load_local(source)

    def cleanup(self) -> None:
        pass


class AsyncLocalLoader(AsyncBaseLoader):
    async def _load(self, source: SourceContent | str, **kwargs: Any) -> LoadedResource:
        self._reject_unsupported(kwargs)
        # 只卸载未加闸的文件计算；公开 load 已持有许可，不能再次申请。
        return await run_sync(_load_local, source)

    async def cleanup(self) -> None:
        pass
