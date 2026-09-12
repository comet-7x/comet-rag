from __future__ import annotations

from importlib import import_module
from typing import Any

from .hooks import HookProvider, HookRegistry, HooksState, PipelineHooks
from .types import Chunk, DocxConfig, PipelineConfig, PipelineResult

Pipeline: Any


def __getattr__(name: str) -> Any:
    if name != "Pipeline":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    Pipeline = import_module("comet_rag.pipeline").Pipeline
    globals()[name] = Pipeline
    return Pipeline

__all__ = [
    "Chunk",
    "HookProvider",
    "HookRegistry",
    "HooksState",
    "DocxConfig",
    "Pipeline",
    "PipelineConfig",
    "PipelineHooks",
    "PipelineResult",
]
