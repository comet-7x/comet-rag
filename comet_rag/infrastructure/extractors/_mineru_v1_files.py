from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO

from comet_rag.infrastructure.extractors._mineru_v1_protocol import (
    CaptureError,
    CaptureSource,
)

CHUNK_BYTES = 64 * 1024


def atomic_json(path: Path, value: dict[str, object]) -> None:
    """回执先落盘再轮询；原子替换不等于与远端提交组成事务。"""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".receipt-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class CaptureFiles:
    """只持有本次新目录及私有输入快照；所有方法由取消安全的线程边界调用。"""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.owned = False
        self.resources = ExitStack()
        self.snapshot: BinaryIO | None = None
        self.artifact: BinaryIO | None = None
        self.part = output / ".structured_content.part"

    def prepare(self, source: CaptureSource, receipt: dict[str, object]) -> None:
        self.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.owned = True
        atomic_json(
            self.output / "source_lock.json",
            {
                "samples": [
                    {
                        "id": Path(source.filename).stem,
                        "path": str(source.path.resolve()),
                        "sha256": source.sha256,
                        "bytes": source.size,
                        "pages": source.pages,
                    }
                ]
            },
        )
        self.save_receipt(receipt)

    def save_receipt(self, value: dict[str, object]) -> None:
        if self.owned:
            atomic_json(self.output / "receipt.json", value)

    def copy_source(self, source: CaptureSource, check: Callable[[], float]) -> None:
        # 快照跨提交和上传存活，由close中的ExitStack统一关闭。
        self.snapshot = self.resources.enter_context(
            tempfile.TemporaryFile(mode="w+b", dir=self.output)  # noqa: SIM115
        )
        size = 0
        digest = sha256()
        descriptor = os.open(source.path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != source.size:
                raise CaptureError("source_not_regular_or_wrong_size")
            while True:
                check()
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > source.size:
                    raise CaptureError("source_size_mismatch")
                digest.update(chunk)
                self.snapshot.write(chunk)
        if size != source.size or digest.hexdigest() != source.sha256:
            raise CaptureError("source_digest_mismatch")
        self.snapshot.seek(0)

    def read_source(self) -> bytes:
        if self.snapshot is None:
            raise RuntimeError("输入快照未准备")
        return self.snapshot.read(CHUNK_BYTES)

    def start_artifact(self) -> None:
        self.artifact = self.part.open("xb")
        os.chmod(self.part, 0o600)

    def write_artifact(self, chunk: bytes) -> None:
        if self.artifact is None:
            raise RuntimeError("产物文件未打开")
        self.artifact.write(chunk)

    def finish_artifact(self) -> None:
        if self.artifact is not None:
            self.artifact.flush()
            os.fsync(self.artifact.fileno())
            self.artifact.close()
            self.artifact = None

    def publish(self, receipt: dict[str, object], check: Callable[[], float]) -> None:
        check()
        os.replace(self.part, self.output / "structured_content.json")
        check()
        self.save_receipt(receipt)
        check()

    def discard_artifact(self) -> None:
        if not self.owned:
            return
        if self.artifact is not None:
            self.artifact.close()
            self.artifact = None
        self.part.unlink(missing_ok=True)
        (self.output / "structured_content.json").unlink(missing_ok=True)

    def close(self) -> None:
        try:
            if self.artifact is not None:
                self.artifact.close()
                self.artifact = None
        finally:
            if self.snapshot is not None:
                self.resources.close()
                self.snapshot = None
