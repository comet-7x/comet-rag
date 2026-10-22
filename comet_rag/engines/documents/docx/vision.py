from __future__ import annotations

import inspect

from comet_rag.ports.vision import AsyncVisionDescriptionPort, VisionDescriptionPort


def validate_vision_model(
    model: VisionDescriptionPort | AsyncVisionDescriptionPort | None,
    *,
    asynchronous: bool,
) -> None:
    """同名 Protocol 无法在运行时区分执行模型；错配必须在联网前拒绝。"""
    if model is None:
        return
    if inspect.iscoroutinefunction(model.describe) != asynchronous:
        expected = (
            "AsyncVisionDescriptionPort" if asynchronous else "VisionDescriptionPort"
        )
        raise TypeError(f"DOCX 此执行路径需要 {expected}，收到 {type(model).__name__}")
