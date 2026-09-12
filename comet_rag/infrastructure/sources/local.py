from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from comet_rag.infrastructure.sources.base import BaseLoader
from comet_rag.infrastructure.sources.file_info import build_file_metadata
from comet_rag.ports.source import LoadedResource, SourceContent

LoaderContent = LoadedResource


class LocalLoader(BaseLoader):
    def _build_metadata(self, source: SourceContent) -> dict[str, Any]:
        return build_file_metadata(Path(source.source), source)

    def _load(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent:
        self._reject_unsupported(kwargs)
        if isinstance(source, str):
            source = SourceContent(source)
        if not source.is_local:
            raise ValueError(
                f"LocalLoader only handles local paths, got: {source.source!r}"
            )
        return LoaderContent(
            path=Path(source.source),
            source=source,
            is_temp=False,
            metadata=self._build_metadata(source),
        )

    async def _aload(self, source: SourceContent | str, **kwargs: Any) -> LoaderContent:
        self._reject_unsupported(kwargs)
        # 调**未加闸**的 `_load`：闸门已由外壳 `aload` 持有。走公开的 `load`
        # 会在工作线程里再申请一次同一份预算 —— 上限为 1 时当场死锁（实测
        # admitted=2 然后挂死），上限大时白白吃掉一半名额。与
        # `AutoLoader.bind_gate` 里说的是同一个失效模式。
        return await asyncio.to_thread(self._load, source)

    def cleanup(self) -> None:
        pass
