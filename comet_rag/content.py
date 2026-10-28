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
            if not isinstance(self.path, (str, Path)) or not str(self.path):
                raise ValueError("media path must be nonempty")
            object.__setattr__(self, "path", Path(self.path))
        if self.url is not None:
            if not isinstance(self.url, str):
                raise TypeError("media URL must be a string")
            parts = urlsplit(self.url)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
            ):
                raise ValueError(
                    "media URL must be HTTP(S) without embedded credentials"
                )
        if self.data is not None:
            if not isinstance(self.data, bytes) or not self.data:
                raise ValueError("media data must be nonempty bytes")
            if not self.media_type:
                raise ValueError("media_type is required for bytes")
        if self.media_type is not None and (
            not isinstance(self.media_type, str) or not self.media_type.strip()
        ):
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
    if not parts or not any(
        isinstance(part, ImagePart) or isinstance(part, TextPart) and part.text
        for part in parts
    ):
        raise ValueError("content must be nonempty")
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
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("item id must be nonblank")
        parts = content_parts(self.content)
        if not isinstance(self.content, str):
            object.__setattr__(self, "content", parts)
