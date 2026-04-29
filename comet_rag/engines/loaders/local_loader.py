import asyncio
from pathlib import Path
from typing import Any

from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.data_type import ParseConfig
from comet_rag.engines.loaders.types import LoaderContent, SourceContent, SourceType


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
            "source_type": source.pre_source_type,
            "file_name": path.name,
            "file_type": file_type,
            "file_size": file_size,
        }
        try:
            metadata["parse_config"] = ParseConfig.from_extension(file_type)
        except ValueError:
            metadata["parse_config"] = None

        return metadata

    def load(self, source: SourceContent | str, **_) -> LoaderContent:
        if isinstance(source, str):
            source = SourceContent(source)
        if source.pre_source_type != SourceType.LOCAL:
            raise ValueError(
                f"LocalLoader only handles local paths, got: {source.source!r}"
            )
        return LoaderContent(
            path=Path(source.source),
            source=source,
            is_temp=False,
            metadata=self._build_metadata(source),
        )

    async def aload(self, source: SourceContent | str, **kwargs) -> LoaderContent:
        return await asyncio.to_thread(self.load, source, **kwargs)

    def cleanup(self) -> None:
        pass
