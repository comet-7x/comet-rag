import os
from abc import ABC, abstractmethod
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from comet_rag.engines.loaders.data_type import ParseConfig
from comet_rag.engines.utils import compute_sha256


class SourceType(StrEnum):
    URL = "url"
    CLOUD = "cloud"
    LOCAL = "local"
    UNKNOWN = "unknown"


class SourceContent:
    def __init__(self, source: str | Path):
        self.source = str(source).strip()

    @cached_property
    def scheme(self) -> str:
        return urlparse(self.source).scheme.lower()

    @cached_property
    def is_url(self) -> bool:
        return self.scheme in ("http", "https")

    @cached_property
    def is_local(self) -> bool:
        if self.is_url:
            return False
        return os.path.exists(self.source)

    @cached_property
    def extension(self) -> str:
        parsed_path = urlparse(self.source).path if self.is_url else self.source
        return Path(parsed_path).suffix.lstrip(".").lower()

    @cached_property
    def parse_config(self) -> ParseConfig:
        return ParseConfig.from_path(self.source)

    @cached_property
    def source_id(self) -> str:
        source_type = self.source_type
        if source_type == SourceType.URL:
            source_to_hash = self.source
        elif source_type == SourceType.LOCAL:
            source_to_hash = os.path.normpath(self.source)
        else:
            source_to_hash = compute_sha256(self.source)
        return compute_sha256(source_to_hash)

    @cached_property
    def source_type(self) -> SourceType:
        if self.is_url:
            return SourceType.URL
        if self.is_local:
            return SourceType.LOCAL
        return SourceType.UNKNOWN


class LoaderResult(BaseModel):
    content: str | bytes = Field(..., description="Loaded content")

    source: str = Field(..., description="Original source")
    source_type: SourceType = Field(..., description="Source type")
    source_id: str = Field(..., description="Unique identifier")

    extension: str | None = Field(None, description="File extension")
    parse_config: ParseConfig | None = Field(None, description="Parse config")

    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseLoader(ABC):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    @classmethod
    def supports(cls, source: SourceContent) -> bool:
        return False

    @abstractmethod
    def load(self, source: SourceContent, **kwargs: Any) -> LoaderResult: ...

    @abstractmethod
    async def aload(self, source: SourceContent, **kwargs: Any) -> LoaderResult: ...

    def batch_load(
        self, sources: list[SourceContent], *, max_concurrency: int = 10, **kwargs: Any
    ) -> list[LoaderResult]:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            futures = [executor.submit(self.load, s, **kwargs) for s in sources]
            return [future.result() for future in futures]

    async def abatch_load(
        self, sources: list[SourceContent], *, max_concurrency: int = 10, **kwargs: Any
    ) -> list[LoaderResult]:
        import asyncio

        semaphore = asyncio.Semaphore(max_concurrency)

        async def _load_with_limit(source: SourceContent) -> LoaderResult:
            async with semaphore:
                return await self.aload(source, **kwargs)

        tasks = [_load_with_limit(s) for s in sources]
        return await asyncio.gather(*tasks)


# TODO 后续添加自动选择加载器
# def auto_loader(source: str):
#     for loader in ALL_LOADERS:
#         if loader.supports(source):
#             return loader()
