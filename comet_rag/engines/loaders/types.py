import os
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from comet_rag.engines.utils import compute_sha256


class SourceContent:
    def __init__(self, source: str | Path):
        self.source = str(source).strip()

    @cached_property
    def parsed_url(self):
        return urlparse(self.source)

    @cached_property
    def is_url(self) -> bool:
        if self.parsed_url.scheme.lower() not in ("http", "https"):
            return False
        return bool(self.parsed_url.netloc)

    @cached_property
    def is_local(self) -> bool:
        if self.is_url:
            return False
        try:
            p = Path(self.source)
            return p.is_file()
        except OSError:
            return False

    @cached_property
    def source_type(self) -> str:
        """Return an extensible source label without enumerating every adapter.

        Built-in filesystem and HTTP sources keep their stable labels. Other URI
        schemes use the scheme itself, so infrastructure adapters such as S3,
        MinIO, GCS, or future custom routes do not require editing an engines enum.
        """

        if self.is_url:
            return "url"
        if self.is_local:
            return "local"
        return self.parsed_url.scheme.lower() or "unknown"

    @cached_property
    def source_id(self) -> str:
        if self.is_local:
            try:
                abs_path = os.path.abspath(self.source)
                source_to_hash = Path(abs_path).as_posix()
            except OSError:
                source_to_hash = self.source
        else:
            source_to_hash = self.source
        return compute_sha256(source_to_hash)


@dataclass
class LoaderContent:
    path: Path
    source: SourceContent
    is_temp: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    _release: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def cleanup(self) -> None:
        release, self._release = self._release, None
        try:
            if self.is_temp:
                self.path.unlink(missing_ok=True)
        finally:
            # Owners may track temporary resources for shutdown cleanup. Notify
            # them exactly once when the consumer releases the content so their
            # bookkeeping does not grow for the lifetime of the process.
            if release is not None:
                release()
