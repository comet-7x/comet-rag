from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from comet_rag._urls import has_url_credentials
from comet_rag.documents.schemas import SourceInfo


@dataclass(frozen=True, slots=True)
class LocalSource:
    path: Path | str
    source_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, (Path, str)) or not str(self.path):
            raise ValueError("path must be a nonempty local path")
        object.__setattr__(self, "path", Path(self.path))
        _source_id(self.source_id)


def _source_id(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError("source_id must be nonblank")


@dataclass(frozen=True, slots=True)
class HttpSource:
    url: str = field(repr=False)
    source_id: str | None = None

    def __post_init__(self) -> None:
        _source_id(self.source_id)
        if not isinstance(self.url, str) or any(ord(char) <= 32 for char in self.url):
            raise ValueError("HTTP source requires an HTTP(S) URL")
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid HTTP source URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (port is not None and not 0 < port <= 65535)
        ):
            raise ValueError(
                "HTTP source requires an HTTP(S) URL without userinfo or fragments"
            )
        if has_url_credentials(self.url, include_key=True) and self.source_id is None:
            raise ValueError("sensitive HTTP query requires explicit source_id")


@dataclass(frozen=True, slots=True)
class S3Source:
    bucket: str
    key: str
    version_id: str | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        _source_id(self.source_id)
        if (
            not isinstance(self.bucket, str)
            or not self.bucket.strip()
            or "/" in self.bucket
            or any(ord(char) <= 32 for char in self.bucket)
        ):
            raise ValueError("S3 bucket must be a nonempty bucket name")
        if not isinstance(self.key, str) or not self.key or "\x00" in self.key:
            raise ValueError("S3 key must be nonempty without NUL")
        if self.version_id is not None and (
            not isinstance(self.version_id, str) or not self.version_id
        ):
            raise ValueError("version_id must be nonempty")


type Source = LocalSource | HttpSource | S3Source


@dataclass(frozen=True, slots=True)
class LocalResource:
    path: Path
    source: SourceInfo
    original_name: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise TypeError("resource path must be a Path")
        if not isinstance(self.source, SourceInfo):
            raise TypeError("resource source must be SourceInfo")
        for name in ("original_name", "media_type"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be nonblank")
        if self.size_bytes is not None and (
            type(self.size_bytes) is not int or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a nonnegative integer")
