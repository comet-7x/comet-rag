import asyncio
from pathlib import Path
from typing import Any

from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.data_type import ParseConfig
from comet_rag.engines.loaders.types import LoaderContent, SourceContent


class LocalLoader(BaseLoader):
    def _build_metadata(self, source: SourceContent) -> dict[str, Any]:
        path = Path(source.source)
        file_type = path.suffix.lstrip(".").lower()
        file_size = path.stat().st_size
        if file_size <= 0:
            raise ValueError(
                f"File is empty and cannot be loaded. Path: {path.resolve()}, type: {file_type}"
            )
        metadata = {
            "source_type": source.source_type,
            "file_name": path.name,
            "file_type": file_type,
            "file_size": file_size,
        }
        try:
            metadata["parse_config"] = ParseConfig.from_extension(file_type)
        except ValueError:
            metadata["parse_config"] = None

        return metadata

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

    async def _aload(
        self, source: SourceContent | str, **kwargs: Any
    ) -> LoaderContent:
        self._reject_unsupported(kwargs)
        # 调**未加闸**的 `_load`：闸门已由外壳 `aload` 持有。走公开的 `load`
        # 会在工作线程里再申请一次同一份预算 —— 上限为 1 时当场死锁（实测
        # admitted=2 然后挂死），上限大时白白吃掉一半名额。与
        # `AutoLoader.bind_gate` 里说的是同一个失效模式。
        return await asyncio.to_thread(self._load, source)

    def cleanup(self) -> None:
        pass
