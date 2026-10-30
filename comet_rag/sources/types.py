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
        if not isinstance(self.path, (Path, str)):
            raise ValueError("path must be a str or Path")
        if not str(self.path):
            raise ValueError("path must be a nonempty local path")
        object.__setattr__(self, "path", Path(self.path))
        _validate_source_id(self.source_id)


def _validate_source_id(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError("source_id must be a string")
    if not value.strip():
        raise ValueError("source_id must be nonblank")


@dataclass(frozen=True, slots=True)
class HttpSource:
    url: str = field(repr=False)
    source_id: str | None = None

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        if not isinstance(self.url, str):
            raise ValueError("url must be a string")
        if any(ord(char) <= 32 for char in self.url):
            raise ValueError("url must not contain whitespace or control characters")
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid HTTP source URL") from exc
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("HTTP source URL must use HTTP(S)")
        if not parsed.hostname:
            raise ValueError("HTTP source URL must have a host")
        if parsed.username is not None:
            raise ValueError("HTTP source URL must not contain userinfo")
        if parsed.password is not None:
            raise ValueError("HTTP source URL must not contain userinfo")
        if parsed.fragment:
            raise ValueError("HTTP source URL must not contain a fragment")
        if port is not None and not 0 < port <= 65535:
            raise ValueError("HTTP source URL port must be in 1..65535")
        if self.source_id is None and has_url_credentials(self.url, include_key=True):
            raise ValueError("sensitive HTTP query requires explicit source_id")


@dataclass(frozen=True, slots=True)
class S3Source:
    bucket: str
    key: str
    version_id: str | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        if not isinstance(self.bucket, str):
            raise ValueError("bucket must be a string")
        if not self.bucket.strip():
            raise ValueError("S3 bucket must be a nonempty bucket name")
        if "/" in self.bucket:
            raise ValueError("S3 bucket must not contain '/'")
        if any(ord(char) <= 32 for char in self.bucket):
            raise ValueError("S3 bucket must not contain whitespace or controls")
        if not isinstance(self.key, str):
            raise ValueError("key must be a string")
        if not self.key:
            raise ValueError("S3 key must be nonempty")
        if "\x00" in self.key:
            raise ValueError("S3 key must not contain NUL")
        if self.version_id is not None:
            if not isinstance(self.version_id, str):
                raise ValueError("version_id must be a string")
            if not self.version_id:
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
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            if not value.strip():
                raise ValueError(f"{name} must be nonblank")
        if self.size_bytes is not None:
            if type(self.size_bytes) is not int:
                raise ValueError("size_bytes must be an integer")
            if self.size_bytes < 0:
                raise ValueError("size_bytes must be nonnegative")
