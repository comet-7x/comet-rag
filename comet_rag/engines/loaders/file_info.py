from __future__ import annotations

import tempfile
import threading
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO, cast

from loguru import logger

from comet_rag.engines.loaders.data_type import (
    ParseConfig,
    is_allowed_extension,
    resolve_detected_extension,
)
from comet_rag.engines.utils.file_detector import detect_content_type_from_path
from comet_rag.ports.source import SourceContent


def build_file_metadata(
    path: str | Path,
    source: SourceContent,
    *,
    file_name: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造所有 Loader 共享的文件事实；来源专有字段由 ``extra`` 追加。"""
    file_path = Path(path)
    file_type = file_path.suffix.lower().lstrip(".")
    file_size = file_path.stat().st_size
    if file_size <= 0:
        raise ValueError(
            "File is empty and cannot be loaded. "
            f"Path: {file_path.resolve()}, type: {file_type}"
        )
    metadata: dict[str, Any] = {
        "source_type": source.source_type,
        "file_name": file_name or file_path.name,
        "file_type": file_type,
        "file_size": file_size,
    }
    try:
        metadata["parse_config"] = ParseConfig.from_extension(file_type)
    except ValueError:
        metadata["parse_config"] = None
    if extra is not None:
        metadata.update(extra)
    return metadata


def resolve_downloaded_extension(path: str | Path, declared_name: str) -> str:
    """以内容探测为准确认扩展名；探测器失效时只接受白名单声明。"""
    declared = Path(declared_name).suffix.lower().lstrip(".")
    try:
        detected = detect_content_type_from_path(str(path)).lower().lstrip(".")
    except Exception as exc:
        if not is_allowed_extension(declared):
            raise ValueError(
                f"Unable to determine content type for {declared_name!r}"
            ) from exc
        logger.warning(f"内容探测失败，回退到声明后缀 {declared!r}: {exc!r}")
        return declared
    return resolve_detected_extension(declared, detected)


class TemporaryFileRegistry:
    """并发安全地登记 Loader 创建、转名和移交的临时文件。"""

    def __init__(self, directory: str | Path | None = None) -> None:
        self._directory = Path(directory) if directory else None
        self._lock = threading.Lock()
        self._paths: list[Path] = []

    @property
    def paths(self) -> list[str]:
        with self._lock:
            return [str(path) for path in self._paths]

    def create(self) -> BinaryIO:
        # NamedTemporaryFile 的公开返回类型未暴露其跨平台 wrapper；调用者只依赖
        # BinaryIO 与运行时存在的字符串 ``name``。
        temporary = tempfile.NamedTemporaryFile(  # noqa: SIM115
            delete=False,
            dir=self._directory,
        )
        with self._lock:
            self._paths.append(Path(temporary.name))
        # typeshed 把 wrapper 与其委托的二进制文件建模为两个不相交类型，运行时
        # wrapper 完整转发这里用到的 BinaryIO 接口。
        return cast(BinaryIO, temporary)

    def track(self, path: str | Path) -> None:
        with self._lock:
            self._paths.append(Path(path))

    def replace_suffix(self, path: str | Path, extension: str) -> str:
        current = Path(path)
        target = current.with_suffix(f".{extension}")
        with self._lock:
            try:
                index = self._paths.index(current)
            except ValueError as exc:
                raise RuntimeError(
                    f"Temporary file is no longer tracked: {current}"
                ) from exc
            # 转名与账本更新必须同锁，避免并发 release 留下已转名的孤儿文件。
            current.replace(target)
            self._paths[index] = target
        return str(target)

    def release(self, path: str | Path) -> None:
        with self._lock, suppress(ValueError):
            self._paths.remove(Path(path))

    def discard(self, path: str | Path) -> None:
        Path(path).unlink(missing_ok=True)
        self.release(path)

    def cleanup(self) -> None:
        with self._lock:
            paths = self._paths.copy()
        first_error: OSError | None = None
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                # 继续清理其余文件，但失败项仍留在账本供下一次 shutdown 重试。
                if first_error is None:
                    first_error = exc
            else:
                self.release(path)
        if first_error is not None:
            raise first_error


__all__ = [
    "TemporaryFileRegistry",
    "build_file_metadata",
    "resolve_downloaded_extension",
]
