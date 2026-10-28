from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from comet_rag._async import run_sync
from comet_rag.documents.schemas import SourceInfo
from comet_rag.identity import stable_id

from .types import LocalResource, LocalSource, Source


class AsyncLocalSourceResolver:
    """只读借用本地文件；调用方保证上下文内原件稳定。"""

    def __init__(self, *, max_bytes: int = 256 * 1024 * 1024) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._max_bytes = max_bytes

    @asynccontextmanager
    async def resolve(self, source: Source) -> AsyncIterator[LocalResource]:
        if not isinstance(source, LocalSource):
            raise TypeError("local resolver requires LocalSource")
        yield await run_sync(self._resolve, source)

    def _resolve(self, source: LocalSource) -> LocalResource:
        path = Path(source.path).expanduser().resolve(strict=True)
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError(f"local source is not a regular file: {path}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"local source is not a regular file: {path}")
            while chunk := stream.read(min(1024 * 1024, self._max_bytes - size + 1)):
                size += len(chunk)
                if size > self._max_bytes:
                    raise ValueError(f"local source exceeds {self._max_bytes} bytes")
                digest.update(chunk)
        locator = path.as_uri()
        return LocalResource(
            path=path,
            source=SourceInfo(
                source.source_id or stable_id("source", {"local": locator}),
                digest.hexdigest(),
                locator=locator,
            ),
            original_name=path.name,
            media_type=mimetypes.guess_type(path.name)[0],
            size_bytes=size,
        )
