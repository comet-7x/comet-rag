from __future__ import annotations

import codecs
from pathlib import Path

from comet_rag._async import run_sync
from comet_rag.documents.schemas import (
    Block,
    Location,
    ParsedDocument,
    ParseResult,
    ParsingInfo,
    RichText,
    TextRun,
)
from comet_rag.sources.types import LocalResource

from .base import _read_resource


class AsyncTextParser:
    def __init__(
        self, *, encoding: str = "utf-8", max_bytes: int = 8 * 1024 * 1024
    ) -> None:
        self._encoding = codecs.lookup(encoding).name
        if type(max_bytes) is not int:
            raise ValueError("max_bytes must be a positive integer")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._max_bytes = max_bytes

    async def parse(self, resource: LocalResource, *, output_dir: Path) -> ParseResult:
        return await run_sync(self._parse, resource)

    def _parse(self, resource: LocalResource) -> ParseResult:
        text = _read_resource(resource, self._max_bytes).decode(
            self._encoding, errors="strict"
        )
        return ParseResult(
            ParsedDocument(
                source=resource.source,
                parsing=ParsingInfo(
                    "native-text", version="1", config={"encoding": self._encoding}
                ),
                blocks=(
                    Block(
                        "text_0",
                        "text",
                        rich_text=RichText((TextRun(text),)),
                        locations=(
                            Location(element_id="text", start=0, end=len(text)),
                        ),
                    ),
                )
                if text
                else (),
            )
        )
