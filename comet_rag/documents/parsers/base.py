from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Protocol

from comet_rag.documents.schemas import ParseResult
from comet_rag.sources.types import LocalResource


class AsyncDocumentParser(Protocol):
    async def parse(
        self, resource: LocalResource, *, output_dir: Path
    ) -> ParseResult: ...


def _read_resource(resource: LocalResource, max_bytes: int) -> bytes:
    if not isinstance(resource, LocalResource):
        raise TypeError("parser requires LocalResource")
    if not stat.S_ISREG(resource.path.stat().st_mode):
        raise ValueError(f"parser input is not a regular file: {resource.path}")
    with resource.path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("parser input must be a regular file")
        if info.st_size > max_bytes:
            raise ValueError(f"parser input exceeds {max_bytes} bytes")
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"parser input exceeds {max_bytes} bytes")
    if resource.size_bytes is not None and len(data) != resource.size_bytes:
        raise ValueError("resource size changed since resolution")
    if hashlib.sha256(data).hexdigest() != resource.source.content_hash:
        raise ValueError("resource bytes do not match source content_hash")
    return data
