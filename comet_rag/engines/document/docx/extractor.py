from __future__ import annotations

from pathlib import Path

from comet_rag.engines.cleaners.docx_cleaner import DocxCleaner
from comet_rag.engines.cleaners.vision_model import VisionModel
from comet_rag.engines.converters.archive_guard import ArchiveLimits
from comet_rag.engines.converters.text_converter import DocxConverter
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.parsers.docx_parser.docx_parser import DocxParser
from comet_rag.ports.document import ExtractedDocument


class DocxDocumentExtractor:
    """在进程内把 DOCX 转成规范化文档。"""

    def __init__(
        self,
        *,
        heading_numbers: bool = False,
        include_images: bool = True,
        include_headers_footers: bool = False,
        vision_model: VisionModel | None = None,
        archive_limits: ArchiveLimits | None = None,
    ) -> None:
        self._heading_numbers = heading_numbers
        self._include_images = include_images
        self._include_headers_footers = include_headers_footers
        self._vision_model = vision_model
        self._archive_limits = archive_limits or ArchiveLimits()

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

    def _cleaner(self) -> DocxCleaner:
        return DocxCleaner(
            include_images=self._include_images,
            include_headers_footers=self._include_headers_footers,
            vision_model=self._vision_model,
        )

    def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        content = self._content(path, filename=filename, media_type=media_type)
        document = DocxConverter(
            content, archive_limits=self._archive_limits
        ).to_docx()
        parsed = self._parser().parse(document)
        markdown = self._cleaner().clean_to_markdown(parsed)
        return ExtractedDocument(markdown=markdown, metadata=dict(parsed.metadata))

    async def aextract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        content = self._content(path, filename=filename, media_type=media_type)
        document = await DocxConverter(
            content, archive_limits=self._archive_limits
        ).ato_docx()
        parsed = await self._parser().aparse(document)
        markdown = await self._cleaner().aclean_to_markdown(parsed)
        return ExtractedDocument(markdown=markdown, metadata=dict(parsed.metadata))

    async def aclose(self) -> None:
        # 提取器只持有配置和调用方注入的视觉模型，不拥有需要关闭的资源。
        return None


__all__ = ["DocxDocumentExtractor"]
