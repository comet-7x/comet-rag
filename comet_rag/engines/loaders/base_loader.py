import os
from abc import ABC, abstractmethod
from functools import cached_property
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from comet_rag.engines.loaders.data_type import ParseConfig
from comet_rag.engines.utils import compute_sha256


class SourceContent:
    CLOUD_STORAGE_SCHEMES: ClassVar[set[str]] = {
        "s3",  # AWS S3
        "gs",  # Google Cloud Storage
        "oss",  # 阿里云OSS
        "cos",  # 腾讯云COS
        "az",  # Azure Blob Storage
        "azure",  # Azure 别名
        "hdfs",  # Hadoop HDFS
        "obs",  # 华为云OBS
        "minio",  # MinIO 对象存储
    }

    def __init__(self, source: str | Path):
        self.source = str(source).strip()

    @cached_property
    def scheme(self) -> str:
        return urlparse(self.source).scheme.lower()

    @cached_property
    def is_url(self) -> bool:
        return self.scheme in ("http", "https")

    @cached_property
    def is_cloud(self) -> bool:
        return self.scheme in self.CLOUD_STORAGE_SCHEMES

    @cached_property
    def is_local(self) -> bool:
        return os.path.exists(self.source)

    @cached_property
    def extension(self) -> str:
        parsed_path = (
            urlparse(self.source).path
            if (self.is_url or self.is_cloud)
            else self.source
        )
        return Path(parsed_path).suffix.lstrip(".").lower()

    @cached_property
    def parse_config(self) -> ParseConfig:
        return ParseConfig.from_path(self.source)

    @cached_property
    def source_id(self) -> str:
        if self.is_url or self.is_cloud:
            source_to_hash = self.source
        elif self.is_local:
            source_to_hash = os.path.normpath(self.source)
        else:
            raise ValueError(f"Invalid source: {self.source}")
        return compute_sha256(source_to_hash)

    @cached_property
    def source_type(self) -> str:
        if self.is_url:
            return "url"
        if self.is_cloud:
            return "cloud"
        if self.is_local:
            return "local"
        return "unknown"


class LoaderResult(BaseModel):
    content: str | bytes = Field(..., description="Loaded content")

    source: str = Field(..., description="Original source")
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
        self, sources: list[SourceContent], *, max_workers: int = 4, **kwargs: Any
    ) -> list[LoaderResult]:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
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
