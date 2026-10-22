from __future__ import annotations

from pathlib import Path

from comet_rag.engines.documents.common.archive import ArchiveLimits
from comet_rag.engines.documents.docx.cleaner import AsyncDocxCleaner, DocxCleaner
from comet_rag.engines.documents.docx.converter import DocxConverter
from comet_rag.engines.documents.docx.parser import DocxParser
from comet_rag.engines.documents.docx.types import DocxParsedContent
from comet_rag.engines.documents.docx.vision import validate_vision_model
from comet_rag.engines.offload import run_sync
from comet_rag.ports.document import ExtractedDocument
from comet_rag.ports.source import LoadedResource, SourceContent
from comet_rag.ports.vision import AsyncVisionDescriptionPort, VisionDescriptionPort

LoaderContent = LoadedResource


class _DocxExtraction:
    _heading_numbers: bool
    _archive_limits: ArchiveLimits

    @staticmethod
    def _content(path: Path, *, filename: str, media_type: str) -> LoaderContent:
        metadata: dict[str, object] = {
            "file_name": filename,
            "file_type": path.suffix.lower().lstrip("."),
            "file_size": path.stat().st_size,
            "media_type": media_type,
        }
        return LoaderContent(
            path=path,
            source=SourceContent(path),
            metadata=metadata,
        )

    def _parser(self) -> DocxParser:
        # Parser 在一次解析期间持有遍历状态；每份文档使用独立实例，避免并发串扰。
        return DocxParser(heading_numbers=self._heading_numbers)

    def _parse(
        self, path: Path, *, filename: str, media_type: str
    ) -> DocxParsedContent:
        content = self._content(path, filename=filename, media_type=media_type)
        document = DocxConverter(content, archive_limits=self._archive_limits).to_docx()
        return self._parser().parse(document)


class DocxDocumentExtractor(_DocxExtraction):
    def __init__(
        self,
        *,
        heading_numbers: bool = False,
        include_images: bool = True,
        include_headers_footers: bool = False,
        vision_model: VisionDescriptionPort | None = None,
        archive_limits: ArchiveLimits | None = None,
    ) -> None:
        self._heading_numbers = heading_numbers
        self._include_images = include_images
        self._include_headers_footers = include_headers_footers
        validate_vision_model(vision_model, asynchronous=False)
        self._vision_model = vision_model
        self._archive_limits = archive_limits or ArchiveLimits()

    def _cleaner(self) -> DocxCleaner:
        return DocxCleaner(
            include_images=self._include_images,
            include_headers_footers=self._include_headers_footers,
            vision_model=self._vision_model,
        )

    def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        parsed = self._parse(path, filename=filename, media_type=media_type)
        markdown = self._cleaner().clean_to_markdown(parsed)
        return ExtractedDocument(markdown=markdown, metadata=dict(parsed.metadata))

    def close(self) -> None:
        # 注入的视觉模型由调用方管理。
        return None


class AsyncDocxDocumentExtractor(_DocxExtraction):
    def __init__(
        self,
        *,
        heading_numbers: bool = False,
        include_images: bool = True,
        include_headers_footers: bool = False,
        vision_model: AsyncVisionDescriptionPort | None = None,
        archive_limits: ArchiveLimits | None = None,
    ) -> None:
        self._heading_numbers = heading_numbers
        self._include_images = include_images
        self._include_headers_footers = include_headers_footers
        validate_vision_model(vision_model, asynchronous=True)
        self._vision_model = vision_model
        self._archive_limits = archive_limits or ArchiveLimits()

    def _cleaner(self) -> AsyncDocxCleaner:
        return AsyncDocxCleaner(
            include_images=self._include_images,
            include_headers_footers=self._include_headers_footers,
            vision_model=self._vision_model,
        )

    async def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        parsed = await run_sync(
            self._parse, path, filename=filename, media_type=media_type
        )
        markdown = await self._cleaner().clean_to_markdown(parsed)
        return ExtractedDocument(markdown=markdown, metadata=dict(parsed.metadata))

    async def close(self) -> None:
        # 注入的视觉模型由调用方管理。
        return None


__all__ = ["DocxDocumentExtractor", "AsyncDocxDocumentExtractor"]
