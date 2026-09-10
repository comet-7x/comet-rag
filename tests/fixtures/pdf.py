"""生成可被内容探测器识别的最小 PDF 测试样本。"""

from __future__ import annotations

import zlib
from pathlib import Path


def _build_pdf(objects: tuple[bytes, ...]) -> bytes:
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode())
        body.extend(value)
        body.extend(b"\nendobj\n")

    xref_offset = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n".encode())
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        (
            f"trailer\n<< /Root 1 0 R /Size {len(offsets)} >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(body)


def _stream(content: bytes, options: bytes = b"") -> bytes:
    suffix = b" " + options if options else b""
    return (
        f"<< /Length {len(content)}".encode()
        + suffix
        + b" >>\nstream\n"
        + content
        + b"\nendstream"
    )


def minimal_pdf_bytes() -> bytes:
    content = b"BT /F1 24 Tf 72 700 Td (Hello PDF) Tj ET"
    return _build_pdf(
        (
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 640 792] "
                b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
            ),
            _stream(content),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        )
    )


_GLYPHS = {
    " ": ("00000",) * 7,
    "2": ("11110", "00001", "00001", "11110", "10000", "10000", "11111"),
    "4": ("10010", "10010", "10010", "11111", "00010", "00010", "00010"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
}


def _raster_text(
    text: str, *, scale: int = 8, padding: int = 16
) -> tuple[int, int, bytes]:
    width = padding * 2 + (len(text) * 6 - 1) * scale
    height = padding * 2 + 7 * scale
    pixels = bytearray([255]) * (width * height)
    for glyph_index, character in enumerate(text):
        glyph = _GLYPHS[character]
        x_offset = padding + glyph_index * 6 * scale
        for row, pattern in enumerate(glyph):
            for column, bit in enumerate(pattern):
                if bit == "0":
                    continue
                for y in range(padding + row * scale, padding + (row + 1) * scale):
                    start = y * width + x_offset + column * scale
                    pixels[start : start + scale] = b"\0" * scale
    return width, height, bytes(pixels)


def scanned_pdf_bytes() -> bytes:
    """只含光栅图片、没有 PDF 文本层的 OCR 样本。"""
    width, height, pixels = _raster_text("SCAN TEST 42")
    image = zlib.compress(pixels)
    image_options = (
        f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
        "/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode"
    ).encode()
    content = f"q {width} 0 0 {height} 18 360 cm /Im0 Do Q".encode()
    return _build_pdf(
        (
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 640 792] "
                b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>"
            ),
            _stream(content),
            _stream(image, image_options),
        )
    )


def build_minimal_pdf(path: Path) -> Path:
    path.write_bytes(minimal_pdf_bytes())
    return path


def build_scanned_pdf(path: Path) -> Path:
    path.write_bytes(scanned_pdf_bytes())
    return path


__all__ = [
    "build_minimal_pdf",
    "build_scanned_pdf",
    "minimal_pdf_bytes",
    "scanned_pdf_bytes",
]
