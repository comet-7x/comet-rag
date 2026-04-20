from pathlib import Path

from comet_rag.engines.loaders.base_loader import BaseLoader, LoaderResult
from comet_rag.engines.loaders.source_content import SourceContent, SourceType


class LocalLoader(BaseLoader):
    def load(self, source: SourceContent | str, **_) -> LoaderResult:
        if isinstance(source, str):
            source = SourceContent(source)
        if source.pre_source_type != SourceType.LOCAL:
            raise ValueError(
                f"LocalLoader only handles local paths, got: {source.source!r}"
            )
        return LoaderResult(path=Path(source.source), source=source, is_temp=False)

    async def aload(self, source: SourceContent | str, **_) -> LoaderResult:
        return self.load(source)

    def cleanup(self) -> None:
        pass
