from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from comet_rag.documents.codec import dumps, loads
from comet_rag.documents.parsers import image as image_module
from comet_rag.documents.parsers.base import AsyncDocumentParser
from comet_rag.documents.parsers.image import AsyncImageParser
from comet_rag.documents.projection import project_document
from comet_rag.documents.schemas import ParseResult, SourceInfo
from comet_rag.sources.types import LocalResource


def _image(path: Path, format: str = "PNG", *, oriented: bool = False) -> LocalResource:
    with Image.new("RGB", (4, 3), (20, 40, 80)) as image:
        if oriented:
            exif = image.getexif()
            exif[274] = 6
            image.save(path, format=format, exif=exif)
        else:
            image.save(path, format=format)
    return _resource(path)


def _resource(path: Path) -> LocalResource:
    data = path.read_bytes()
    return LocalResource(
        path=path,
        source=SourceInfo(
            "logical-image",
            hashlib.sha256(data).hexdigest(),
            source_version="fixture-v1",
        ),
        original_name=path.name,
        media_type="application/octet-stream",
        size_bytes=len(data),
    )


def _parse(resource: LocalResource, output_dir: Path, **limits: int) -> ParseResult:
    parser: AsyncDocumentParser = AsyncImageParser(**limits)
    return asyncio.run(parser.parse(resource, output_dir=output_dir))


@pytest.mark.parametrize(
    ("format", "media_type", "extension"),
    [("PNG", "image/png", ".png"), ("JPEG", "image/jpeg", ".jpg")],
)
def test_image_parser_preserves_actual_bytes_and_maps_complete_pixel_canvas(
    tmp_path: Path, format: str, media_type: str, extension: str
) -> None:
    resource = _image(tmp_path / "misleading-name.txt", format)
    original = resource.path.read_bytes()
    before = resource.path.stat()
    result = _parse(
        resource, tmp_path / "assets", max_bytes=len(original), max_pixels=12
    )
    document = result.document
    assert document.source is resource.source
    assert result.report.status == "complete" and result.report.issues == ()
    assert result.report.coverage == {"total_frames": 1, "processed_frames": (0,)}
    assert document.parsing.engine == "pillow-image" and document.parsing.version
    assert document.parsing.config == {
        "mapping_version": 1,
        "coordinates": "raw-pixels-top-left-v1",
        "frames": "single",
        "exif_orientation": "preserved",
        "ocr": False,
    }
    (asset,) = document.assets
    (block,) = document.blocks
    (page,) = document.pages
    digest = hashlib.sha256(original).hexdigest()
    assert asset.asset_id == f"asset_{digest}"
    assert asset.content_hash == digest
    assert asset.provenance == "original" and asset.source_asset_ids == ()
    assert asset.media_type == asset.media.media_type == media_type
    assert asset.width == 4 and asset.height == 3 and asset.size_bytes == len(original)
    assert isinstance(asset.media.path, Path)
    assert asset.media.path == tmp_path / "assets" / f"{digest}{extension}"
    assert asset.media.path.read_bytes() == original
    assert asset.media.data is None and asset.media.url is None
    assert block.kind == "image" and block.asset_id == asset.asset_id
    assert block.alt_text is None and block.rich_text is None
    (location,) = block.locations
    assert (location.page_id, location.bbox, location.unit, location.origin) == (
        "page_1",
        (0, 0, 4, 3),
        "px",
        "top-left",
    )
    assert location.coordinate_space == "source" and location.coordinate_version == 1
    assert (
        page.page_id,
        page.number,
        page.width,
        page.height,
        page.unit,
        page.asset_id,
    ) == ("page_1", 1, 4, 3, "px", asset.asset_id)
    projection = project_document(document)
    assert projection.text == ""
    assert [(item.block_id, item.code) for item in projection.limitations] == [
        (block.block_id, "image_without_text")
    ]
    assert loads(dumps(result)) == result
    assert resource.path.read_bytes() == original
    assert resource.path.stat().st_mtime_ns == before.st_mtime_ns
    assert resource.path.stat().st_ino == before.st_ino
    resource.path.unlink()
    with Image.open(asset.media.path) as stored:
        stored.load()
        assert stored.format == format and stored.size == (4, 3)


def test_image_parser_is_idempotent_and_output_paths_do_not_change_document_identity(
    tmp_path: Path,
) -> None:
    resource = _image(tmp_path / "source.png")
    first = _parse(resource, tmp_path / "first")
    asset = first.document.assets[0]
    assert isinstance(asset.media.path, Path)
    before = asset.media.path.stat()
    same = _parse(resource, tmp_path / "first")
    different = _parse(resource, tmp_path / "second")
    assert first == same
    assert first.document.document_id == different.document.document_id
    assert asset.media.path.stat().st_mtime_ns == before.st_mtime_ns
    assert asset.media.path.stat().st_ino == before.st_ino
    renamed = replace(resource, original_name="different.jpg")
    assert (
        _parse(renamed, tmp_path / "third").document.document_id
        == first.document.document_id
    )


def test_jpeg_exif_rotation_keeps_raw_pixel_coordinates_and_encoded_bytes(
    tmp_path: Path,
) -> None:
    resource = _image(tmp_path / "oriented.jpg", "JPEG", oriented=True)
    result = _parse(resource, tmp_path / "assets")
    asset = result.document.assets[0]
    assert (asset.width, asset.height) == (4, 3)
    assert result.document.blocks[0].locations[0].bbox == (0, 0, 4, 3)
    assert isinstance(asset.media.path, Path)
    assert asset.media.path.read_bytes() == resource.path.read_bytes()
    with Image.open(asset.media.path) as image:
        assert image.getexif()[274] == 6


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an image",
        b"\x89PNG\r\n\x1a\n",
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    ],
)
def test_invalid_or_unsupported_bytes_fail_without_outputs(
    tmp_path: Path, data: bytes
) -> None:
    path = tmp_path / "fake.png"
    path.write_bytes(data)
    resource = _resource(path)
    with pytest.raises(ValueError, match="pillow-image.*logical-image") as caught:
        _parse(resource, tmp_path / "assets")
    assert caught.value.__cause__ is not None
    assert not (tmp_path / "assets").exists()
    assert path.read_bytes() == data


@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_container_verification_and_full_decode_reject_truncation(
    tmp_path: Path, format: str
) -> None:
    resource = _image(tmp_path / "truncated", format)
    data = resource.path.read_bytes()
    resource.path.write_bytes(data[:-8] if format == "PNG" else data[:-2])
    resource = _resource(resource.path)
    with pytest.raises(ValueError, match="pillow-image"):
        _parse(resource, tmp_path / "assets")
    assert not (tmp_path / "assets").exists()


def test_bad_png_crc_is_rejected_before_materialization(tmp_path: Path) -> None:
    resource = _image(tmp_path / "crc.png")
    data = bytearray(resource.path.read_bytes())
    data[-5] ^= 1
    resource.path.write_bytes(data)
    with pytest.raises(ValueError, match="pillow-image"):
        _parse(_resource(resource.path), tmp_path / "assets")
    assert not (tmp_path / "assets").exists()


@pytest.mark.parametrize("format", ["GIF", "TIFF", "PNG"])
def test_multi_frame_images_are_rejected_explicitly(
    tmp_path: Path, format: str
) -> None:
    path = tmp_path / "animated"
    with (
        Image.new("RGB", (4, 3), "red") as first,
        Image.new("RGB", (4, 3), "blue") as second,
    ):
        first.save(path, format=format, save_all=True, append_images=[second])
    resource = _resource(path)
    data = path.read_bytes()
    with pytest.raises(ValueError, match="multi-frame"):
        _parse(resource, tmp_path / "assets")
    assert not (tmp_path / "assets").exists()
    assert path.read_bytes() == data


@pytest.mark.parametrize("format", ["GIF", "TIFF", "BMP", "WEBP"])
def test_single_frame_supported_raster_formats_remain_original(
    tmp_path: Path, format: str
) -> None:
    resource = _image(tmp_path / "raster", format)
    result = _parse(resource, tmp_path / "assets")
    assert result.report.status == "complete"
    asset = result.document.assets[0]
    assert (
        asset.media_type
        == {
            "GIF": "image/gif",
            "TIFF": "image/tiff",
            "BMP": "image/bmp",
            "WEBP": "image/webp",
        }[format]
    )
    assert isinstance(asset.media.path, Path)
    assert asset.media.path.read_bytes() == resource.path.read_bytes()


def test_byte_size_and_hash_checks_run_before_decoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = _image(tmp_path / "source.png")
    data = resource.path.read_bytes()
    opened: list[object] = []

    def open_image(*args: object, **kwargs: object) -> None:
        opened.append(args)
        pytest.fail("image decoder must not run on invalid resource bytes")

    monkeypatch.setattr(image_module.Image, "open", open_image)
    with pytest.raises(ValueError, match="exceeds"):
        _parse(resource, tmp_path / "assets", max_bytes=len(data) - 1)
    unknown_size = replace(resource, size_bytes=None)
    with pytest.raises(ValueError, match="exceeds"):
        _parse(unknown_size, tmp_path / "assets", max_bytes=len(data) - 1)
    with pytest.raises(ValueError, match="size changed"):
        _parse(replace(resource, size_bytes=len(data) + 1), tmp_path / "assets")
    resource.path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(ValueError, match="content_hash"):
        _parse(resource, tmp_path / "assets")
    assert opened == []
    assert not (tmp_path / "assets").exists()


def test_pixel_limit_rejects_before_decode_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = _image(tmp_path / "source.png")

    def load_image(*args: object, **kwargs: object) -> None:
        pytest.fail("oversized image must not decode pixels")

    monkeypatch.setattr(Image.Image, "load", load_image)
    with pytest.raises(ValueError, match="11 pixels"):
        _parse(resource, tmp_path / "assets", max_pixels=11)
    assert not (tmp_path / "assets").exists()


@pytest.mark.parametrize(
    "conflict", ["different", "too_large", "symlink", "directory", "hardlink"]
)
def test_output_conflicts_are_never_overwritten(tmp_path: Path, conflict: str) -> None:
    resource = _image(tmp_path / "source.png")
    original = resource.path.read_bytes()
    output = tmp_path / "assets"
    output.mkdir()
    destination = output / f"{resource.source.content_hash}.png"
    existing = b"keep existing bytes"
    if conflict == "symlink":
        target = tmp_path / "existing"
        target.write_bytes(original)
        destination.symlink_to(target)
    elif conflict == "hardlink":
        destination.hardlink_to(resource.path)
    elif conflict == "directory":
        destination.mkdir()
    else:
        destination.write_bytes(
            existing if conflict == "different" else original + b"extra"
        )
    with pytest.raises(ValueError, match="conflicts"):
        _parse(resource, output)
    assert resource.path.read_bytes() == original
    if conflict == "directory":
        assert destination.is_dir()
    elif conflict == "symlink":
        assert destination.is_symlink() and destination.read_bytes() == original
    else:
        assert destination.read_bytes() == (
            original
            if conflict == "hardlink"
            else existing
            if conflict == "different"
            else original + b"extra"
        )


def test_input_already_at_destination_cannot_be_returned_as_owned_output(
    tmp_path: Path,
) -> None:
    resource = _image(tmp_path / "source.png")
    original = resource.path.read_bytes()
    destination = tmp_path / f"{resource.source.content_hash}.png"
    resource.path.rename(destination)
    resource = replace(resource, path=destination)
    with pytest.raises(ValueError, match="conflicts with parser input"):
        _parse(resource, tmp_path)
    assert destination.read_bytes() == original


def test_cancellation_drains_reader_and_materialization_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = _image(tmp_path / "source.png")
    original = resource.path.read_bytes()
    release = threading.Event()
    finished = threading.Event()
    read_resource = image_module._read_resource

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()

        def blocked_read(item: LocalResource, max_bytes: int) -> bytes:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
            data = read_resource(item, max_bytes)
            finished.set()
            return data

        monkeypatch.setattr(image_module, "_read_resource", blocked_read)
        output = tmp_path / "assets"
        task = asyncio.create_task(
            AsyncImageParser().parse(resource, output_dir=output)
        )
        try:
            await asyncio.wait_for(started.wait(), 3)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert resource.path.read_bytes() == original
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set()
            stored = output / f"{resource.source.content_hash}.png"
            assert stored.read_bytes() == original
            with Image.open(stored) as image:
                image.load()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert resource.path.read_bytes() == original

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_image_parser_requires_positive_integer_limits(limit: int) -> None:
    with pytest.raises(ValueError):
        AsyncImageParser(max_bytes=limit)
    with pytest.raises(ValueError):
        AsyncImageParser(max_pixels=limit)
