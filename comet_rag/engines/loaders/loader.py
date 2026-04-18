import asyncio
import io
import logging
import tempfile
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from comet_rag.engines.loaders.data_type import ParseConfig
from comet_rag.engines.loaders.source_content import SourceContent, SourceType
from comet_rag.engines.utils.file_detector import detect_content_type_from_stream

logger = logging.getLogger(__name__)


class LoaderResult(BaseModel):
    content: str | None = Field(..., description="Local file path")
    source: str = Field(..., description="Original source (URL or local path)")
    source_id: str = Field(..., description="Unique identifier")
    metadata: dict[str, Any] = Field(default_factory=dict)


class Loader:
    def __init__(self, download_dir: str | Path | None = None) -> None:
        self.download_dir = Path(download_dir) if download_dir else None

    def _download_from_url(self, url: str, kwargs: dict[str, Any]) -> str:
        headers = kwargs.get(
            "headers",
            {"User-Agent": f"Mozilla/5.0 (compatible; comet-rag {__class__.__name__})"},
        )
        data = kwargs.get("data")
        params = kwargs.get("params")
        try:
            method = "POST" if data else "GET"
            response = httpx.request(
                method,
                url,
                headers=headers,
                data=data,
                params=params,
                timeout=30,
                follow_redirects=True,
            )
            response.raise_for_status()
            label = detect_content_type_from_stream(io.BytesIO(response.content))
            with tempfile.NamedTemporaryFile(
                suffix=f".{label}", delete=False, dir=self.download_dir
            ) as tmp:
                tmp.write(response.content)
                return tmp.name
        except Exception as e:
            raise ValueError(f"Download failed from {url}: {e!s}") from e

    async def _adownload_from_url(self, url: str, kwargs: dict[str, Any]) -> str:
        headers = kwargs.get(
            "headers",
            {"User-Agent": f"Mozilla/5.0 (compatible; comet-rag {__class__.__name__})"},
        )
        data = kwargs.get("data")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                method = "POST" if data else "GET"
                response = await client.request(method, url, headers=headers, data=data)
                response.raise_for_status()

            content = response.content
            loop = asyncio.get_running_loop()
            label = await loop.run_in_executor(
                None, lambda: detect_content_type_from_stream(io.BytesIO(content))
            )
            with tempfile.NamedTemporaryFile(
                suffix=f".{label}", delete=False, dir=self.download_dir
            ) as tmp:
                tmp.write(content)
                return tmp.name
        except Exception as e:
            raise ValueError(f"Async download failed from {url}: {e!s}") from e

    @staticmethod
    def _build_metadata(
        file_path: str | None, source_type: SourceType
    ) -> dict[str, Any]:
        metadata = {}
        metadata["source_type"] = source_type
        metadata["is_temp"] = source_type != SourceType.LOCAL
        if file_path:
            p = Path(file_path)
            metadata["file_name"] = p.name
            metadata["file_type"] = p.suffix.lstrip(".")
            metadata["file_size"] = p.stat().st_size
            metadata["parse_config"] = ParseConfig.from_extension(metadata["file_type"])
        return metadata

    def load(self, source: SourceContent, **kwargs) -> LoaderResult:
        source_type = source.pre_source_type
        if source_type == SourceType.URL:
            file_path = self._download_from_url(source.source, kwargs)
        elif source_type == SourceType.LOCAL:
            file_path = source.source
        else:
            logging.warning(
                f"The source is unsupported or cannot be accessed: {source.source!r}"
            )
            file_path = None

        return LoaderResult(
            content=file_path,
            source=source.source,
            source_id=source.source_id,
            metadata=self._build_metadata(file_path, source_type),
        )

    async def aload(self, source: SourceContent, **kwargs) -> LoaderResult:
        source_type = source.pre_source_type
        if source_type == SourceType.URL:
            file_path = await self._adownload_from_url(source.source, kwargs)
        elif source_type == SourceType.LOCAL:
            file_path = source.source
        else:
            logging.warning(
                f"The source is unsupported or cannot be accessed: {source.source!r}"
            )
            file_path = None

        return LoaderResult(
            content=file_path,
            source=source.source,
            source_id=source.source_id,
            metadata=self._build_metadata(file_path, source_type),
        )

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
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _load_with_limit(source: SourceContent) -> LoaderResult:
            async with semaphore:
                return await self.aload(source, **kwargs)

        return await asyncio.gather(*[_load_with_limit(s) for s in sources])
