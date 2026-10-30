from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class MediaRef:
    path: Path | str | None = None
    url: str | None = None
    data: bytes | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        if sum(value is not None for value in (self.path, self.url, self.data)) != 1:
            raise ValueError("media requires exactly one of path, url or data")
        if self.path is not None:
            if not isinstance(self.path, (str, Path)):
                raise ValueError("media path must be a str or Path")
            if not str(self.path):
                raise ValueError("media path must be nonempty")
            object.__setattr__(self, "path", Path(self.path))
        if self.url is not None:
            if not isinstance(self.url, str):
                raise TypeError("media URL must be a string")
            if self.url != self.url.strip(" "):
                raise ValueError("media URL cannot have surrounding spaces")
            if any(ord(char) < 32 or ord(char) == 127 for char in self.url):
                raise ValueError("media URL cannot contain control characters")
            parts = urlsplit(self.url)
            if parts.scheme not in {"http", "https"}:
                raise ValueError("media URL must use HTTP(S)")
            if not parts.hostname:
                raise ValueError("media URL must have a host")
            if parts.username is not None:
                raise ValueError("media URL must not embed credentials")
            if parts.password is not None:
                raise ValueError("media URL must not embed credentials")
            port = parts.port
            if port is not None:
                if not 0 < port <= 65535:
                    raise ValueError("media URL port must be in 1..65535")
        if self.data is not None:
            if not isinstance(self.data, bytes):
                raise ValueError("media data must be bytes")
            if not self.data:
                raise ValueError("media data must be nonempty")
            if not self.media_type:
                raise ValueError("media_type is required for bytes")
        if self.media_type is None:
            return
        if not isinstance(self.media_type, str):
            raise ValueError("media_type must be a string")
        if not self.media_type.strip():
            raise ValueError("media_type must be nonblank")


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")


@dataclass(frozen=True, slots=True)
class ImagePart:
    resource: MediaRef

    def __post_init__(self) -> None:
        if not isinstance(self.resource, MediaRef):
            raise TypeError("image resource must be a MediaRef")


type ContentPart = TextPart | ImagePart
type ContentInput = str | Sequence[ContentPart]


def content_parts(content: ContentInput) -> tuple[ContentPart, ...]:
    if isinstance(content, str):
        parts: tuple[ContentPart, ...] = (TextPart(content),)
    elif isinstance(content, Sequence):
        parts = tuple(content)
    else:
        raise TypeError("content must be text or an ordered sequence of parts")
    if not parts:
        raise ValueError("content must be nonempty")
    carries_content = any(
        isinstance(part, ImagePart) or (isinstance(part, TextPart) and part.text)
        for part in parts
    )
    if not carries_content:
        raise ValueError("content must contain a nonempty part")
    if any(not isinstance(part, (TextPart, ImagePart)) for part in parts):
        raise TypeError("unsupported content part")
    return parts


def text_view(content: ContentInput) -> str:
    return "".join(
        part.text for part in content_parts(content) if isinstance(part, TextPart)
    )


@dataclass(frozen=True, slots=True)
class ContentItem:
    id: str
    content: ContentInput

    def __post_init__(self) -> None:
        if not isinstance(self.id, str):
            raise ValueError("item id must be a string")
        if not self.id.strip():
            raise ValueError("item id must be nonblank")
        parts = content_parts(self.content)
        if not isinstance(self.content, str):
            object.__setattr__(self, "content", parts)
