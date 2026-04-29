import asyncio
import io
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict

from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.data_type import AllowExt, ParseConfig
from comet_rag.engines.loaders.types import LoaderContent, SourceContent, SourceType
from comet_rag.engines.utils.file_detector import detect_content_type_from_stream


class DownloadRequestConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    headers: dict[str, str] | None = None
    content: bytes | None = None
    data: dict[str, Any] | None = None
    params: dict[str, str] | None = None
    timeout: int = 30
    follow_redirects: bool = True


class URLLoader(BaseLoader):
    def __init__(self, download_dir: str | Path | None = None) -> None:
        self.download_dir = Path(download_dir) if download_dir else None
        self._temp_files: list[str] = []

    @property
    def temp_files(self) -> list[str]:
        return self._temp_files.copy()

    def _build_metadata(self, file_path: str, source: SourceContent) -> dict[str, Any]:
        path = Path(file_path)
        file_type = path.suffix.lstrip(".").lower()
        metadata = {
            "source_type": source.pre_source_type,
            "file_name": path.name,
            "file_type": file_type,
            "file_size": path.stat().st_size,
        }
        try:
            metadata["parse_config"] = ParseConfig.from_extension(file_type)
        except ValueError:
            metadata["parse_config"] = None

        return metadata

    def _download(
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
                raw = _do_request(client)
            else:
                with httpx.Client(
                    timeout=config.timeout, follow_redirects=config.follow_redirects
                ) as c:
                    raw = _do_request(c)
        except Exception as e:
            raise ValueError(f"Download failed from {url}: {e!s}") from e

        if not raw:
            raise ValueError(f"Downloaded empty content from {url}")

        url_ext = Path(urlparse(url).path).suffix.lstrip(".").lower()
        label = (
            url_ext
            if url_ext in AllowExt._value2member_map_
            else detect_content_type_from_stream(io.BytesIO(raw))
        )
        with tempfile.NamedTemporaryFile(
            suffix=f".{label}", delete=False, dir=self.download_dir
        ) as tmp:
            self._temp_files.append(tmp.name)
            tmp.write(raw)
            return tmp.name

    async def _adownload(
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
                raw = await _do_request(client)
            else:
                async with httpx.AsyncClient(
                    timeout=config.timeout, follow_redirects=config.follow_redirects
                ) as c:
                    raw = await _do_request(c)
        except Exception as e:
            raise ValueError(f"Async download failed from {url}: {e!s}") from e

        if not raw:
            raise ValueError(f"Downloaded empty content from {url}")

        url_ext = Path(urlparse(url).path).suffix.lstrip(".").lower()
        loop = asyncio.get_running_loop()
        if url_ext in AllowExt._value2member_map_:
            label = url_ext
        else:
            label = await loop.run_in_executor(
                None, lambda: detect_content_type_from_stream(io.BytesIO(raw))
            )

        def _save() -> str:
            with tempfile.NamedTemporaryFile(
                suffix=f".{label}", delete=False, dir=self.download_dir
            ) as tmp:
                self._temp_files.append(tmp.name)
                tmp.write(raw)
                return tmp.name

        return await loop.run_in_executor(None, _save)

    def load(
        self,
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.Client | None = None,
        **kwargs,
    ) -> LoaderContent:
        if isinstance(source, str):
            source = SourceContent(source)
        if source.pre_source_type != SourceType.URL:
            raise ValueError(f"URLLoader only handles URLs, got: {source.source!r}")
        config = download_config or DownloadRequestConfig()
        file_path = self._download(source.source, config, client)
        return LoaderContent(
            path=Path(file_path),
            source=source,
            is_temp=True,
            metadata=self._build_metadata(file_path, source),
        )

    async def aload(
        self,
        source: SourceContent | str,
        *,
        download_config: DownloadRequestConfig | None = None,
        client: httpx.AsyncClient | None = None,
        **kwargs,
    ) -> LoaderContent:
        if isinstance(source, str):
            source = SourceContent(source)
        if source.pre_source_type != SourceType.URL:
            raise ValueError(f"URLLoader only handles URLs, got: {source.source!r}")
        config = download_config or DownloadRequestConfig()
        file_path = await self._adownload(source.source, config, client)
        loop = asyncio.get_running_loop()
        metadata = await loop.run_in_executor(
            None, self._build_metadata, file_path, source
        )
        return LoaderContent(
            path=Path(file_path), source=source, is_temp=True, metadata=metadata
        )

    def batch_load(
        self,
        sources: list[SourceContent] | list[str],
        download_config: DownloadRequestConfig | None = None,
        max_concurrency: int = 10,
        **kwargs,
    ) -> list[LoaderContent]:
        from concurrent.futures import ThreadPoolExecutor

        config = download_config or DownloadRequestConfig()
        with (
            httpx.Client(
                timeout=config.timeout, follow_redirects=config.follow_redirects
            ) as client,
            ThreadPoolExecutor(max_workers=max_concurrency) as executor,
        ):
            futures = [
                executor.submit(
                    self.load, s, download_config=config, client=client, **kwargs
                )
                for s in sources
            ]
            return [f.result() for f in futures]

    async def abatch_load(
        self,
        sources: list[SourceContent] | list[str],
        download_config: DownloadRequestConfig | None = None,
        max_concurrency: int = 10,
        **kwargs,
    ) -> list[LoaderContent]:
        config = download_config or DownloadRequestConfig()
        semaphore = asyncio.Semaphore(max_concurrency)
        async with httpx.AsyncClient(
            timeout=config.timeout, follow_redirects=config.follow_redirects
        ) as client:

            async def _load(source: SourceContent | str) -> LoaderContent:
                async with semaphore:
                    return await self.aload(
                        source, download_config=config, client=client, **kwargs
                    )

            return await asyncio.gather(*[_load(s) for s in sources])

    def cleanup(self) -> None:
        for path in self._temp_files:
            Path(path).unlink(missing_ok=True)
        self._temp_files.clear()
