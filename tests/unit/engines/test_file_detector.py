from __future__ import annotations

import warnings
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest

from comet_rag.engines.utils.file_detector import (
    detect_content_type_from_bytes,
    detect_content_type_from_path,
    detect_content_type_from_stream,
)


@pytest.mark.parametrize(
    "detect",
    [
        lambda path: detect_content_type_from_bytes(path.read_bytes()),
        lambda path: detect_content_type_from_path(str(path)),
        lambda path: detect_content_type_from_stream(BytesIO(path.read_bytes())),
    ],
)
def test_magika_current_label_api_is_used_without_deprecation_warning(
    tmp_path: Path, detect: Callable[[Path], str]
) -> None:
    """Magika 已用 `label` 取代 `ct_label`；三种入口都不能退回旧属性。"""
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert detect(path) == "txt"
