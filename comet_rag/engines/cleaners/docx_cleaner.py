import asyncio
import base64
from pathlib import Path
from typing import Protocol

from loguru import logger

from comet_rag.engines.cleaners.base_cleaner import BaseCleaner
from comet_rag.engines.parsers.types import Block, DocxParsedContent


class VisionModel(Protocol):
    def describe(self, base64_data: str, media_type: str, **kwargs) -> str: ...

    async def adescribe(self, base64_data: str, media_type: str, **kwargs) -> str: ...


class DocxCleaner(BaseCleaner):
    def __init__(
        self,
        include_headers_footers: bool = False,
        include_images: bool = True,
        vision_model: VisionModel | None = None,
    ):
        self._include_headers_footers = include_headers_footers
        self._include_images = include_images
        self._vision_model = vision_model

    def clean_to_markdown(
        self,
        parse_content: DocxParsedContent,
        output_dir: Path | None = None,
        filename: str = "result",
    ) -> str:
        parts: list[str] = []
        image_blocks: list[Block] = []
        for block in self.clean_to_blocks(parse_content):
            if block.get("type") == "image":
                image_blocks.append(block)
                if self._vision_model is not None:
                    text = self._describe_image_block_sync(block)
                else:
                    text = self._block_to_markdown(block)
            else:
                text = self._block_to_markdown(block)
            if text:
                parts.append(text)
        result = "\n\n".join(parts)
        if output_dir is not None:
            self._write_markdown_and_images(result, output_dir, filename, image_blocks)
        return result

    async def aclean_to_markdown(
        self,
        parse_content: DocxParsedContent,
        output_dir: Path | None = None,
        filename: str = "result",
    ) -> str:
        if self._vision_model is None:
            return await asyncio.to_thread(
                self.clean_to_markdown, parse_content, output_dir, filename
            )
        blocks = await asyncio.to_thread(self.clean_to_blocks, parse_content)
        results = await asyncio.gather(*(self._process_block_markdown(b) for b in blocks))
        result = "\n\n".join(r for r in results if r)
        if output_dir is not None:
            image_blocks = [b for b in blocks if b.get("type") == "image"]
            await asyncio.to_thread(
                self._write_markdown_and_images, result, output_dir, filename, image_blocks
            )
        return result

    def clean_to_blocks(self, parse_content: DocxParsedContent) -> list[Block]:
        result: list[Block] = []
        for block in parse_content.blocks:
            btype = block.get("type", "")
            if btype in ("header", "footer") and not self._include_headers_footers:
                continue
            if btype == "image" and not self._include_images:
                continue
            result.append(block)
        return result

    async def aclean_to_blocks(self, parse_content: DocxParsedContent) -> list[Block]:
        return await asyncio.to_thread(self.clean_to_blocks, parse_content)

    def _describe_image_block_sync(self, block: Block) -> str:
        content = block.get("content", "")
        fmt = block.get("format", "png")
        alt = block.get("alt_text") or block.get("name", "")
        if not content or self._vision_model is None:
            return f"[image: {alt}]" if alt else "[image]"
        try:
            return self._vision_model.describe(content, f"image/{fmt}")
        except Exception as exc:
            logger.warning(f"Vision model failed for image '{alt}': {exc}")
            return f"[image: {alt}]" if alt else "[image]"

    async def _describe_image_block_async(self, block: Block) -> str:
        content = block.get("content", "")
        fmt = block.get("format", "png")
        alt = block.get("alt_text") or block.get("name", "")
        if not content or self._vision_model is None:
            return f"[image: {alt}]" if alt else "[image]"
        try:
            return await self._vision_model.adescribe(content, f"image/{fmt}")
        except Exception as exc:
            logger.warning(f"Vision model failed for image '{alt}': {exc}")
            return f"[image: {alt}]" if alt else "[image]"

    async def _process_block(self, block: Block) -> str:
        if block.get("type") == "image":
            return await self._describe_image_block_async(block)
        return self._block_to_text(block)

    async def _process_block_markdown(self, block: Block) -> str:
        if block.get("type") == "image":
            return await self._describe_image_block_async(block)
        return self._block_to_markdown(block)

    def _write_markdown_and_images(
        self,
        result: str,
        output_dir: Path,
        filename: str,
        image_blocks: list[Block],
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{filename}.md").write_text(result, encoding="utf-8")
        if image_blocks:
            images_dir = output_dir / "images"
            images_dir.mkdir(exist_ok=True)
            for block in image_blocks:
                self._save_image(block, images_dir)

    def _block_to_text(self, block: Block) -> str:
        btype = block.get("type", "")
        content = block.get("content", "")

        if btype == "heading":
            level = max(1, min(block.get("level", 1), 6))
            number = block.get("number", "")
            text = f"{number} {content}" if number else content
            return f"{'#' * level} {text}"

        if btype in ("text", "caption", "equation", "header", "footer"):
            return content

        if btype == "table":
            return content

        if btype == "image":
            alt = block.get("alt_text") or block.get("name", "")
            return f"[image: {alt}]" if alt else "[image]"

        if btype == "list":
            return self._list_to_text(block, indent=0)

        return content

    def _block_to_markdown(self, block: Block) -> str:
        if block.get("type") == "image":
            alt = block.get("alt_text") or block.get("name", "")
            img_id = block.get("id", "")
            fmt = block.get("format", "png")
            path = f"images/{img_id}.{fmt}" if img_id else ""
            return f"![{alt}]({path})"
        return self._block_to_text(block)

    def _save_image(self, block: Block, images_dir: Path) -> None:
        img_id = block.get("id", "")
        fmt = block.get("format", "png")
        content = block.get("content", "")
        if not img_id or not content:
            return
        (images_dir / f"{img_id}.{fmt}").write_bytes(base64.b64decode(content))

    def _list_to_text(self, block: Block, indent: int) -> str:
        is_ordered = block.get("attribute", "unordered") == "ordered"
        prefix = "  " * indent
        lines: list[str] = []
        for i, item in enumerate(block.get("content") or [], start=1):
            if item.get("type") == "list":
                lines.append(self._list_to_text(item, indent + 1))
            else:
                marker = f"{i}." if is_ordered else "-"
                lines.append(f"{prefix}{marker} {item.get('content', '')}")
        return "\n".join(lines)
