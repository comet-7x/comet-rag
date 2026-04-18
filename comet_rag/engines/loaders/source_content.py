import os
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from urllib.parse import urlparse

from comet_rag.engines.utils import compute_sha256


class SourceType(StrEnum):
    URL = "url"
    LOCAL = "local"
    UNKNOWN = "unknown"


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
            return p.exists()
        except OSError:
            return False

    @cached_property
    def source_id(self) -> str:
        pre_source_type = self.pre_source_type
        if pre_source_type == SourceType.URL:
            source_to_hash = self.source
        elif pre_source_type == SourceType.LOCAL:
            abs_path = os.path.abspath(self.source)
            source_to_hash = Path(abs_path).as_posix()
        else:
            source_to_hash = self.source
        return compute_sha256(source_to_hash)

    @cached_property
    def pre_source_type(self) -> SourceType:
        if self.is_url:
            return SourceType.URL
        if self.is_local:
            return SourceType.LOCAL
        return SourceType.UNKNOWN
