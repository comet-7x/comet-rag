import asyncio
import io
import logging
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from comet_rag.engines.loaders.data_type import AllowExt, ParseConfig
from comet_rag.engines.loaders.source_content import SourceContent, SourceType
from comet_rag.engines.utils.file_detector import detect_content_type_from_stream

logger = logging.getLogger(__name__)


class DownloadRequestConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    headers: dict[str, str] | None = None
    content: bytes | None = None
    data: dict[str, Any] | None = None
    params: dict[str, str] | None = None
    timeout: int = 30
    follow_redirects: bool = True


class LoaderResult(BaseModel):
    local_path: str | None = Field(..., description="Local file path")
    source: str = Field(..., description="Original source (URL or local path)")
    source_id: str = Field(..., description="Unique identifier")
    metadata: dict[str, Any] = Field(default_factory=dict)

    def cleanup(self) -> None:
        if self.metadata.get("is_temp") and self.local_path:
            Path(self.local_path).unlink(missing_ok=True)


class Loader:
    def __init__(self, download_dir: str | Path | None = None) -> None:
        self.download_dir = Path(download_dir) if download_dir else None
        self._temp_files: list[str] = []

    @property
    def temp_files(self) -> list[str]:
        return self._temp_files.copy()

    def _download_from_url(
        self,
        url: str,
        config: DownloadRequestConfig,
        client: httpx.Client | None = None,
    ) -> str:
        headers = config.headers or {
            "User-Agent": f"Mozilla/5.0 (compatible; comet-rag {type(self).__name__})"
        }

        def _do_request(c: httpx.Client) -> bytes:
            method = "POST" if (config.content or config.data) else "GET"
            response = c.request(
                method,
                url,
                headers=headers,
                content=config.content,
                data=config.data,
                params=config.params,
                timeout=config.timeout,
                follow_redirects=config.follow_redirects,
            )
            response.raise_for_status()
            return response.content

        try:
            if client is not None:
                content = _do_request(client)
            else:
                with httpx.Client(
                    timeout=config.timeout, follow_redirects=config.follow_redirects
                ) as c:
                    content = _do_request(c)
            url_ext = Path(urlparse(url).path).suffix.lstrip(".")
            label = (
                url_ext
                if url_ext in AllowExt._value2member_map_
                else detect_content_type_from_stream(io.BytesIO(content))
            )
            with tempfile.NamedTemporaryFile(
                suffix=f".{label}", delete=False, dir=self.download_dir
            ) as tmp:
                tmp.write(content)
                self._temp_files.append(tmp.name)
                return tmp.name
        except Exception as e:
            raise ValueError(f"Download failed from {url}: {e!s}") from e

    async def _adownload_from_url(
        self,
        url: str,
        config: DownloadRequestConfig,
        client: httpx.AsyncClient | None = None,
    ) -> str:
        headers = config.headers or {
            "User-Agent": f"Mozilla/5.0 (compatible; comet-rag {type(self).__name__})"
        }

        async def _do_request(c: httpx.AsyncClient) -> bytes:
            method = "POST" if (config.content or config.data) else "GET"
            response = await c.request(
                method,
                url,
                headers=headers,
                content=config.content,
                data=config.data,
                params=config.params,
                timeout=config.timeout,
                follow_redirects=config.follow_redirects,
            )
            response.raise_for_status()
            return response.content

        try:
            if client is not None:
                content = await _do_request(client)
            else:
                async with httpx.AsyncClient(
                    timeout=config.timeout, follow_redirects=config.follow_redirects
                ) as c:
                    content = await _do_request(c)

            url_ext = Path(urlparse(url).path).suffix.lstrip(".")
            loop = asyncio.get_running_loop()
            if url_ext in AllowExt._value2member_map_:
                label = url_ext
            else:
                label = await loop.run_in_executor(
                    None, lambda: detect_content_type_from_stream(io.BytesIO(content))
                )

            def _save_temp():
                with tempfile.NamedTemporaryFile(
                    suffix=f".{label}", delete=False, dir=self.download_dir
                ) as tmp:
                    tmp.write(content)
                    return tmp.name

            file_path = await loop.run_in_executor(None, _save_temp)
            self._temp_files.append(file_path)
            return file_path
        except Exception as e:
            raise ValueError(f"Async download failed from {url}: {e!s}") from e

    @staticmethod
    def _build_metadata(
        file_path: str | None, source_type: SourceType
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "source_type": source_type,
            "is_temp": source_type != SourceType.LOCAL,
        }
        if file_path:
            p = Path(file_path)
            metadata["file_name"] = p.name
            metadata["file_type"] = p.suffix.lstrip(".")
            metadata["file_size"] = p.stat().st_size
            metadata["parse_config"] = ParseConfig.from_extension(metadata["file_type"])
        return metadata

    def load(
        self,
        source: SourceContent,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        data: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout: int = 30,
        follow_redirects: bool = True,
        client: httpx.Client | None = None,
    ) -> LoaderResult:
        source_type = source.pre_source_type
        if source_type == SourceType.URL:
            config = DownloadRequestConfig(
                headers=headers,
                content=content,
                data=data,
                params=params,
                timeout=timeout,
                follow_redirects=follow_redirects,
            )
            file_path = self._download_from_url(source.source, config, client)
        elif source_type == SourceType.LOCAL:
            file_path = source.source
        else:
            logger.warning(
                "The source is unsupported or cannot be accessed: %r", source.source
            )
            file_path = None

        return LoaderResult(
            local_path=file_path,
            source=source.source,
            source_id=source.source_id,
            metadata=self._build_metadata(file_path, source_type),
        )

    async def aload(
        self,
        source: SourceContent,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        data: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout: int = 30,
        follow_redirects: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> LoaderResult:
        source_type = source.pre_source_type
        if source_type == SourceType.URL:
            config = DownloadRequestConfig(
                headers=headers,
                content=content,
                data=data,
                params=params,
                timeout=timeout,
                follow_redirects=follow_redirects,
            )
            file_path = await self._adownload_from_url(source.source, config, client)
        elif source_type == SourceType.LOCAL:
            file_path = source.source
        else:
            logger.warning(
                "The source is unsupported or cannot be accessed: %r", source.source
            )
            file_path = None

        loop = asyncio.get_running_loop()
        metadata = await loop.run_in_executor(
            None, self._build_metadata, file_path, source_type
        )
        return LoaderResult(
            local_path=file_path,
            source=source.source,
            source_id=source.source_id,
            metadata=metadata,
        )

    def batch_load(
        self,
        sources: list[SourceContent],
        *,
        max_concurrency: int = 10,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        data: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout: int = 30,
        follow_redirects: bool = True,
    ) -> list[LoaderResult]:
        from concurrent.futures import ThreadPoolExecutor

        with (
            httpx.Client(timeout=timeout, follow_redirects=follow_redirects) as client,
            ThreadPoolExecutor(max_workers=max_concurrency) as executor,
        ):
            futures = [
                executor.submit(
                    self.load,
                    s,
                    headers=headers,
                    content=content,
                    data=data,
                    params=params,
                    timeout=timeout,
                    follow_redirects=follow_redirects,
                    client=client,
                )
                for s in sources
            ]
            return [future.result() for future in futures]

    async def abatch_load(
        self,
        sources: list[SourceContent],
        *,
        max_concurrency: int = 10,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        data: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout: int = 30,
        follow_redirects: bool = True,
    ) -> list[LoaderResult]:
        semaphore = asyncio.Semaphore(max_concurrency)

        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=follow_redirects
        ) as client:

            async def _load_with_limit(source: SourceContent) -> LoaderResult:
                async with semaphore:
                    return await self.aload(
                        source,
                        headers=headers,
                        content=content,
                        data=data,
                        params=params,
                        timeout=timeout,
                        follow_redirects=follow_redirects,
                        client=client,
                    )

            return await asyncio.gather(*[_load_with_limit(s) for s in sources])

    def cleanup(self) -> None:
        for path in self._temp_files:
            Path(path).unlink(missing_ok=True)
        self._temp_files.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cleanup()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.cleanup()
