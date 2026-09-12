from __future__ import annotations

from comet_rag.engines.converters.base_converter import BaseConverter
from comet_rag.ports.source import LoadedResource

LoaderContent = LoadedResource


class TextConverter(BaseConverter):
    pass


__all__ = ["TextConverter"]
