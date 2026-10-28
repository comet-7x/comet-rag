from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from comet_rag.engines.documents.mineru.structured import DEFAULT_LIMITS

VERSION = "4.0.12"
FORMAT = "structured_content"
RUNNING = {"queued", "running"}
TERMINAL = {"completed", "partial", "failed", "canceled"}


class CaptureError(RuntimeError):
    """只暴露本地错误码及已观察的ID，不回显上游正文、URL或凭据。"""

    def __init__(self, code: str, task_id: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.task_id = task_id


@dataclass(frozen=True, slots=True)
class CaptureSource:
    path: Path
    filename: str
    sha256: str
    size: int
    pages: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,123}\.pdf", self.filename):
            raise ValueError("采集文件名必须是单一安全PDF文件名")
        digest(self.sha256)
        integer(self.size)
        integer(self.pages)
        if self.pages > DEFAULT_LIMITS.pages:
            raise ValueError("源物理页数超出映射预算")

    def identity(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "bytes": self.size,
            "pages": self.pages,
        }


@dataclass(frozen=True, slots=True)
class CaptureLimits:
    source_bytes: int = 100 * 1024 * 1024
    control_bytes: int = 256 * 1024
    artifact_bytes: int = 32 * 1024 * 1024
    received_bytes: int = 36 * 1024 * 1024
    total_seconds: float = 900.0
    request_seconds: float = 60.0
    upload_seconds: float = 300.0
    poll_seconds: float = 1.0
    cancel_seconds: float = 5.0
    gate_seconds: float = 30.0

    def __post_init__(self) -> None:
        for key in (
            "source_bytes",
            "control_bytes",
            "artifact_bytes",
            "received_bytes",
        ):
            integer(getattr(self, key))
        for key in (
            "total_seconds",
            "request_seconds",
            "upload_seconds",
            "poll_seconds",
            "cancel_seconds",
            "gate_seconds",
        ):
            value = getattr(self, key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{key} 必须是有限正数")
        if self.cancel_seconds + self.poll_seconds >= self.total_seconds:
            raise ValueError("总预算须覆盖正常工作及取消预留")
        if self.artifact_bytes > DEFAULT_LIMITS.wire_bytes:
            raise ValueError("产物接收上限不能超过既有映射预算")
        if max(self.control_bytes, self.artifact_bytes) > self.received_bytes:
            raise ValueError("单次接收预算不能超过累计预算")


def object_value(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CaptureError("protocol_object")
    return cast(dict[str, object], value)


def array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise CaptureError("protocol_array")
    return value


def integer(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise CaptureError("protocol_integer")
    return value


def identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value
    ):
        raise CaptureError("protocol_identifier")
    return value


def digest(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CaptureError("protocol_digest")
    return value


def check_health(value: dict[str, object]) -> None:
    features = object_value(value.get("features"))
    if (
        value.get("status") != "ok"
        or value.get("version") != VERSION
        or FORMAT not in array(features.get("output_formats"))
    ):
        raise CaptureError("unsupported_server")


def check_tiers(value: dict[str, object]) -> None:
    if value.get("object") != "list" or not any(
        object_value(t).get("id") == "advanced" for t in array(value.get("data"))
    ):
        raise CaptureError("unsupported_tier")


@dataclass(frozen=True, slots=True)
class TaskFrame:
    task_id: str
    status: str
    source_file_id: str
    artifact_id: str | None = None
    artifact_bytes: int | None = None


def task_frame(
    value: dict[str, object],
    source: CaptureSource,
    base_url: str,
    *,
    expected_id: str,
    source_file_id: str | None,
) -> TaskFrame:
    task_id = identifier(value.get("task_id"))
    status = value.get("status")
    if (
        task_id != expected_id
        or not isinstance(status, str)
        or status not in RUNNING | TERMINAL
    ):
        raise CaptureError("task_identity_or_status")
    route = f"/v1/tasks/{task_id}"
    links = object_value(value.get("links"))
    for hint, suffix in (
        (value.get("status_url"), ""),
        (value.get("result_url"), "/result"),
        (links.get("self"), ""),
        (links.get("cancel"), ""),
    ):
        if hint not in (route + suffix, base_url + route + suffix):
            raise CaptureError("unexpected_task_link")
    if value.get("tier") != "advanced" or value.get("output_formats") != [FORMAT]:
        raise CaptureError("unexpected_task_options")
    files = array(value.get("files"))
    if len(files) != 1:
        raise CaptureError("unexpected_file_count")
    file = object_value(files[0])
    file_id = identifier(file.get("file_id"))
    if file.get("name") != source.filename or (
        source_file_id is not None and file_id != source_file_id
    ):
        raise CaptureError("source_file_mismatch")
    if status != "completed":
        return TaskFrame(task_id, cast(str, status), file_id)
    page_range = "1" if source.pages == 1 else f"1-{source.pages}"
    if (
        file.get("status") != "completed"
        or file.get("page_range") != page_range
        or object_value(file.get("parse")).get("parser_version") != VERSION
    ):
        raise CaptureError("completed_file_mismatch")
    progress = object_value(value.get("progress"))
    if any(
        type(progress.get(k)) is not int or progress[k] != n
        for k, n in (("total", 1), ("completed", 1), ("failed", 0))
    ):
        raise CaptureError("completed_progress_mismatch")
    outputs = object_value(file.get("output_files"))
    if any(v is not None for k, v in outputs.items() if k != FORMAT):
        raise CaptureError("unexpected_artifact_format")
    output = object_value(outputs.get(FORMAT))
    output_id = identifier(output.get("file_id"))
    if output_id == file_id:
        raise CaptureError("artifact_is_source_file")
    return TaskFrame(
        task_id, "completed", file_id, output_id, integer(output.get("bytes"))
    )


def file_digest(
    value: dict[str, object], *, file_id: str, filename: str, size: int, purpose: str
) -> str:
    if (
        value.get("id") != file_id
        or value.get("object") != "file"
        or value.get("filename") != filename
        or value.get("purpose") != purpose
        or integer(value.get("bytes")) != size
    ):
        raise CaptureError("file_metadata_mismatch")
    return digest(value.get("sha256sum"))
