import asyncio
import base64
from pathlib import Path

from loguru import logger

from comet_rag.engines.cleaners.base_cleaner import BaseCleaner
from comet_rag.engines.cleaners.vision_model import VisionModel
from comet_rag.engines.parsers.types import Block, DocxParsedContent

_IMAGE_MIME_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}


def _sanitize_path_part(value: object, *, field: str) -> str:
    part = str(value)
    if (
        not part
        or part in {".", ".."}
        or "\x00" in part
        or "/" in part
        or "\\" in part
        or Path(part).is_absolute()
        or Path(part).name != part
    ):
        raise ValueError(f"非法的 {field} 路径片段：{part!r}")
    return part


def _image_format(value: object) -> tuple[str, str]:
    extension = str(value or "png").lower().lstrip(".")
    try:
        return extension, _IMAGE_MIME_TYPES[extension]
    except KeyError as exc:
        raise ValueError(f"不支持的图片格式：{value!r}") from exc


class DocxCleaner(BaseCleaner):
    def __init__(
        self,
        include_headers_footers: bool = False,
        include_images: bool = True,
        vision_model: VisionModel | None = None,
    ):
        """
        Initialize a DocxCleaner for converting DOCX content to Markdown with configurable filtering and optional vision-based image descriptions.

        Parameters:
            include_headers_footers (bool): Whether to include header and footer blocks. Defaults to False.
            include_images (bool): Whether to include image blocks. Defaults to True.
            vision_model (VisionModel | None): Optional vision model for generating image descriptions. Defaults to None.
        """
        self._include_headers_footers = include_headers_footers
        self._include_images = include_images
        self._vision_model = vision_model

    def clean_to_markdown(
        self,
        parse_content: DocxParsedContent,
        output_dir: Path | None = None,
        filename: str = "result",
    ) -> str:
        """
        Convert parsed document content to Markdown with optional vision-based image descriptions and file output.

        Converts each document block to Markdown text. Image blocks are described using the configured vision model if available; otherwise, placeholders are used. When output_dir is specified, writes the Markdown to a file and saves any images to disk.

        Parameters:
            parse_content: The document content to convert.
            output_dir: Directory for saving the Markdown file and images. If None, no output files are written.
            filename: Name for the output Markdown file, without extension.

        Returns:
            The generated Markdown string.
        """
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
        """
        Generate markdown from parsed DOCX content, optionally describing images and writing output files.

        When a vision model is configured, processes image blocks using the model's async method. Otherwise, falls back to synchronous processing in a background thread.

        If an output directory is specified, writes the markdown to `{filename}.md` and saves decoded images to `images/`.

        Parameters:
            parse_content (DocxParsedContent): The parsed DOCX content.
            output_dir (Path | None): Directory to write output files. If None, only returns the markdown string.
            filename (str): Base name for the markdown file without extension. Defaults to "result".

        Returns:
            str: The generated markdown string.
        """
        if self._vision_model is None:
            return await asyncio.to_thread(
                self.clean_to_markdown, parse_content, output_dir, filename
            )
        blocks = self.clean_to_blocks(parse_content)
        results = await asyncio.gather(
            *(self._process_block_markdown(b) for b in blocks)
        )
        result = "\n\n".join(r for r in results if r)
        if output_dir is not None:
            image_blocks = [b for b in blocks if b.get("type") == "image"]
            await asyncio.to_thread(
                self._write_markdown_and_images,
                result,
                output_dir,
                filename,
                image_blocks,
            )
        return result

    def clean_to_blocks(self, parse_content: DocxParsedContent) -> list[Block]:
        """
        Filter blocks from parsed content based on configuration flags.

        Returns:
            A list of blocks, excluding headers, footers, and images according to the configured filters.
        """
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
        """
        Filters blocks from parsed content according to the configuration.

        Headers, footers, or images are excluded based on the include_headers_footers and include_images settings.

        Returns:
            A list of filtered blocks.
        """
        return await asyncio.to_thread(self.clean_to_blocks, parse_content)

    def _describe_image_block_sync(self, block: Block) -> str:
        """
        Generate a text description for an image block.

        Returns:
            A text description from the vision model, or a placeholder string in the form "[image]" or "[image: alt_text]".
        """
        content = block.get("content", "")
        fmt = block.get("format", "png")
        alt = block.get("alt_text") or block.get("name", "")
        if not content or self._vision_model is None:
            return f"[image: {alt}]" if alt else "[image]"
        _, mime_type = _image_format(fmt)
        try:
            return self._vision_model.describe(content, mime_type)
        except Exception as exc:
            logger.warning(f"Vision model failed for image '{alt}': {exc}")
            return f"[image: {alt}]" if alt else "[image]"

    async def _describe_image_block_async(self, block: Block) -> str:
        """
        Generate a text description for an image block.

        Uses a vision model to produce a description if available; returns a placeholder
        if the model is unavailable or fails.

        Returns:
            str: A text description of the image.
        """
        content = block.get("content", "")
        fmt = block.get("format", "png")
        alt = block.get("alt_text") or block.get("name", "")
        if not content or self._vision_model is None:
            return f"[image: {alt}]" if alt else "[image]"
        _, mime_type = _image_format(fmt)
        try:
            return await self._vision_model.adescribe(content, mime_type)
        except Exception as exc:
            logger.warning(f"Vision model failed for image '{alt}': {exc}")
            return f"[image: {alt}]" if alt else "[image]"

    async def _process_block_markdown(self, block: Block) -> str:
        """
        Convert a block to Markdown, using image descriptions for image blocks.

        Returns:
                str: A Markdown representation of the block.
        """
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
        """
        Write markdown content to a file and save associated images to disk.

        Creates the output directory if it does not exist and saves all image blocks to an images subdirectory.
        """
        safe_filename = _sanitize_path_part(filename, field="filename")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{safe_filename}.md").write_text(result, encoding="utf-8")
        if image_blocks:
            images_dir = output_dir / "images"
            images_dir.mkdir(exist_ok=True)
            for block in image_blocks:
                self._save_image(block, images_dir)

    def _block_to_text(self, block: Block) -> str:
        """
        Convert a block into its text representation based on type.

        Returns:
            str: Text representation of the block formatted according to its type.
        """
        btype = block.get("type", "")
        content = block.get("content") or ""

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
        """
        Convert a block to Markdown format.

        Image blocks are converted to Markdown image syntax, or a placeholder if the image lacks an ID. Other block types are converted to plain text.

        Returns:
            str: The Markdown representation of the block.
        """
        if block.get("type") == "image":
            alt = block.get("alt_text") or block.get("name", "")
            img_id = block.get("id", "")
            if not img_id:
                return f"[image: {alt}]" if alt else "[image]"
            safe_id = _sanitize_path_part(img_id, field="image id")
            fmt, _ = _image_format(block.get("format", "png"))
            path = f"images/{safe_id}.{fmt}"
            return f"![{alt}]({path})"
        return self._block_to_text(block)

    def _save_image(self, block: Block, images_dir: Path) -> None:
        """
        Writes an image block's base64-encoded content to a file in the images directory.

        If the block lacks an image ID or content, returns without writing. On decoding or I/O errors, logs a warning and does not raise an exception.
        """
        img_id = block.get("id", "")
        fmt = block.get("format", "png")
        content = block.get("content", "")
        if not img_id or not content:
            return
        safe_id = _sanitize_path_part(img_id, field="image id")
        fmt, _ = _image_format(fmt)
        try:
            (images_dir / f"{safe_id}.{fmt}").write_bytes(
                base64.b64decode(content, validate=True)
            )
        except Exception as exc:
            logger.warning(f"Failed to save image '{img_id}.{fmt}': {exc}")

    def _list_to_text(self, block: Block, indent: int) -> str:
        """
        Convert a list block to formatted text with proper indentation and markers.

        Parameters:
            block: A list block with "attribute" (ordered/unordered) and "content" fields.
            indent: The indentation level for nested lists.

        Returns:
            Formatted list as text with bullets, numbers, and line breaks.
        """
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
