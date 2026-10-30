from __future__ import annotations

import hashlib
import subprocess  # nosec B404
import sys
from enum import IntEnum
from pathlib import Path
from typing import Any

import pytest

from comet_rag.content import (
    ContentItem,
    ImagePart,
    MediaRef,
    TextPart,
    content_parts,
    text_view,
)
from comet_rag.identity import canonical_json, file_hash, stable_id


def test_media_construction_is_pure_and_requires_one_source(tmp_path: Path) -> None:
    missing = tmp_path / "never-created.png"
    assert MediaRef(path=str(missing)).path == missing
    assert not missing.exists()
    for values in (
        {},
        {"path": missing, "url": "https://example.test/a"},
        {"data": b"png"},
        {"data": b"", "media_type": "image/png"},
    ):
        with pytest.raises(ValueError):
            MediaRef(**values)  # type: ignore[arg-type]
    assert MediaRef(data=b"png", media_type="image/png").data == b"png"
    for url in ("https://user@example.test/a", "https://@example.test/a"):
        with pytest.raises(ValueError):
            MediaRef(url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org:bad/image.png",
        "https://example.org:65536/image.png",
        "https://example.org:0/image.png",
        "https://example.org/\nimage.png",
        "https://example.org/\timage.png",
        "https://example.org/\x7fimage.png",
        " https://example.org/image.png",
    ],
)
def test_media_rejects_malformed_urls(url: str) -> None:
    with pytest.raises(ValueError):
        MediaRef(url=url)


@pytest.mark.parametrize(
    "values",
    [
        {"path": 42},
        {"data": bytearray(b"x"), "media_type": "image/png"},
        {"path": "image.png", "media_type": 42},
    ],
)
def test_invalid_media_parameters_preserve_value_error(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        MediaRef(**values)


def test_invalid_content_item_id_preserves_value_error() -> None:
    with pytest.raises(ValueError):
        ContentItem(42, "text")  # type: ignore[arg-type]


def test_media_preserves_valid_url_without_reading_it() -> None:
    url = "https://example.invalid:8443/an image.png?edition=2"
    assert MediaRef(url=url).url == url


def test_order_whitespace_and_input_ownership() -> None:
    image = ImagePart(MediaRef(data=b"image", media_type="image/png"))
    parts = [TextPart("  text\n"), image, TextPart(" tail ")]
    item = ContentItem("rep", parts)
    parts.reverse()
    assert item.content == (TextPart("  text\n"), image, TextPart(" tail "))
    assert text_view(item.content) == "  text\n tail "
    assert content_parts("  \n") == (TextPart("  \n"),)
    assert ContentItem("text", " x ").content == " x "
    for empty in ("", [], [TextPart("")]):
        with pytest.raises(ValueError):
            ContentItem("id", empty)
    with pytest.raises(ValueError):
        ContentItem(" ", "text")


def test_canonical_encoding_has_independent_expected_bytes() -> None:
    assert (
        canonical_json({"z": [1, "中"], "a": True})
        == '{"a":true,"z":[1,"中"]}'.encode()
    )
    expected = hashlib.sha256(
        b'{"kind":"unit","value":{"a":1},"version":1}'
    ).hexdigest()
    assert stable_id("unit", {"a": 1}) == f"unit_{expected}"
    assert stable_id("unit", {"a": 1, "b": 2}) == stable_id("unit", {"b": 2, "a": 1})
    assert stable_id("unit", ["a", "b"]) != stable_id("unit", ["b", "a"])
    assert stable_id("unit", {"a": 1}) != stable_id("unit", {"a": 1}, version=2)
    assert stable_id("unit", {"a": 1}) != stable_id("document", {"a": 1})


def test_identity_version_accepts_integer_subclasses() -> None:
    class Version(IntEnum):
        ONE = 1

    expected = (
        "unit_" + hashlib.sha256(b'{"kind":"unit","value":{},"version":1}').hexdigest()
    )
    assert stable_id("unit", {}, version=Version.ONE) == expected


@pytest.mark.parametrize("version", [True, 1.5, "1", None, 0, -1])
def test_invalid_identity_version_preserves_value_error(version: Any) -> None:
    with pytest.raises(ValueError):
        stable_id("unit", {}, version=version)


def test_invalid_identity_kind_preserves_value_error() -> None:
    with pytest.raises(ValueError):
        stable_id(42, {})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), {1: "key"}, {"unordered"}, Path("private")]
)
def test_invalid_identity_values_fail(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        stable_id("unit", value)


def test_hash_is_of_delivered_bytes_and_survives_process_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.txt"
    path.write_bytes(b"actual source bytes\x00")
    assert file_hash(path) == hashlib.sha256(b"actual source bytes\x00").hexdigest()
    code = "from comet_rag.identity import stable_id; print(stable_id('scope', {'items':['a','b']}))"
    # 两个全新进程仅执行当前解释器与固定身份脚本。
    first = subprocess.check_output([sys.executable, "-c", code], text=True)  # noqa: S603  # nosec B603
    second = subprocess.check_output([sys.executable, "-c", code], text=True)  # noqa: S603  # nosec B603
    assert first == second == stable_id("scope", {"items": ["a", "b"]}) + "\n"
