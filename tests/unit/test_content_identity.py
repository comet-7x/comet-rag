from __future__ import annotations

import hashlib
import subprocess  # nosec B404 - fixed Python commands for process-restart verification
import sys
from pathlib import Path

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
    first = subprocess.check_output([sys.executable, "-c", code], text=True)  # noqa: S603  # nosec B603 - fixed interpreter and source
    second = subprocess.check_output([sys.executable, "-c", code], text=True)  # noqa: S603  # nosec B603 - fixed interpreter and source
    assert first == second == stable_id("scope", {"items": ["a", "b"]}) + "\n"
