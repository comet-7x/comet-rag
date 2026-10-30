from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError, __version__

from comet_rag._async import run_sync
from comet_rag.content import MediaRef
from comet_rag.documents.schemas import (
    Asset,
    Block,
    Location,
    PageInfo,
    ParsedDocument,
    ParseReport,
    ParseResult,
    ParsingInfo,
)
from comet_rag.sources.types import LocalResource

from .base import _read_resource

_FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
    "BMP": ("image/bmp", ".bmp"),
    "TIFF": ("image/tiff", ".tiff"),
}


class AsyncImageParser:
    """保留单帧原图；输入由调用方借用，取消时等待解码和转存完成。"""

    def __init__(
        self, *, max_bytes: int = 32 * 1024 * 1024, max_pixels: int = 40_000_000
    ) -> None:
        for name, value in (("max_bytes", max_bytes), ("max_pixels", max_pixels)):
            if type(value) is not int:
                raise ValueError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._max_bytes = max_bytes
        self._max_pixels = max_pixels

    async def parse(self, resource: LocalResource, *, output_dir: Path) -> ParseResult:
        return await run_sync(self._parse, resource, output_dir)

    def _parse(self, resource: LocalResource, output_dir: Path) -> ParseResult:
        data = _read_resource(resource, self._max_bytes)
        try:
            with Image.open(BytesIO(data), formats=tuple(_FORMATS)) as image:
                width, height = image.size
                if width <= 0:
                    raise ValueError(f"image exceeds {self._max_pixels} pixels")
                if height <= 0:
                    raise ValueError(f"image exceeds {self._max_pixels} pixels")
                if width * height > self._max_pixels:
                    raise ValueError(f"image exceeds {self._max_pixels} pixels")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("multi-frame images are not supported")
                media_type, extension = _FORMATS[image.format or ""]
                image.verify()
            # verify 检查容器；重新打开并解码像素才能发现截断的图像数据。
            with Image.open(BytesIO(data), formats=tuple(_FORMATS)) as image:
                image.load()
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"pillow-image cannot parse source {resource.source.source_id!r}: {exc}"
            ) from exc
        content_hash = resource.source.content_hash
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir.resolve() / f"{content_hash}{extension}"
        if destination == resource.path.resolve():
            raise ValueError("asset output conflicts with parser input")
        try:
            with destination.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            if destination.is_symlink():
                raise ValueError(
                    f"asset output conflicts with existing file: {destination}"
                ) from None
            if not destination.is_file():
                raise ValueError(
                    f"asset output conflicts with existing file: {destination}"
                ) from None
            if destination.samefile(resource.path):
                raise ValueError(
                    f"asset output conflicts with existing file: {destination}"
                ) from None
            with destination.open("rb") as stream:
                if stream.read(len(data) + 1) != data:
                    raise ValueError(
                        f"asset output conflicts with existing file: {destination}"
                    ) from None
        asset = Asset(
            asset_id=f"asset_{content_hash}",
            content_hash=content_hash,
            media=MediaRef(path=destination, media_type=media_type),
            media_type=media_type,
            name=resource.original_name,
            size_bytes=len(data),
            width=width,
            height=height,
            provenance="original",
        )
        return ParseResult(
            ParsedDocument(
                source=resource.source,
                parsing=ParsingInfo(
                    "pillow-image",
                    version=__version__,
                    config={
                        "mapping_version": 1,
                        "coordinates": "raw-pixels-top-left-v1",
                        "frames": "single",
                        "exif_orientation": "preserved",
                        "ocr": False,
                    },
                ),
                blocks=(
                    Block(
                        "image_0",
                        "image",
                        asset_id=asset.asset_id,
                        locations=(
                            Location(
                                page_id="page_1",
                                element_id="image",
                                bbox=(0, 0, width, height),
                                unit="px",
                                origin="top-left",
                            ),
                        ),
                    ),
                ),
                pages=(
                    PageInfo(
                        "page_1",
                        number=1,
                        width=width,
                        height=height,
                        unit="px",
                        asset_id=asset.asset_id,
                    ),
                ),
                assets=(asset,),
            ),
            ParseReport(coverage={"total_frames": 1, "processed_frames": (0,)}),
        )


__all__ = ["AsyncImageParser"]
