from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager, contextmanager
from pathlib import Path
from typing import Protocol

from comet_rag.identity import canonical_json

from .types import LocalResource, Source


class AsyncSourceResolver(Protocol):
    def resolve(self, source: Source) -> AbstractAsyncContextManager[LocalResource]: ...


class SourceError(ValueError):
    """来源获取或完整性校验失败；消息不包含认证定位。"""


class SourceResourceLimitExceeded(SourceError):
    """交付字节或上游声明大小超过来源预算。"""


@contextmanager
def _temporary_directory(parent: Path | None) -> Iterator[Path]:
    directory = tempfile.TemporaryDirectory(prefix="comet-source-", dir=parent)
    try:
        yield Path(directory.name)
    except BaseException as exc:
        try:
            directory.cleanup()
        except OSError as cleanup:
            exc.add_note(f"source temporary directory cleanup failed: {cleanup}")
        raise
    else:
        directory.cleanup()


def _positive_limit(name: str, value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _content_length(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    if type(value) is int and value >= 0:
        return value
    raise SourceError("invalid upstream Content-Length")


def _version(values: dict[str, object]) -> str | None:
    nonempty = {key: value for key, value in values.items() if value is not None}
    return canonical_json(nonempty).decode("utf-8") if nonempty else None
