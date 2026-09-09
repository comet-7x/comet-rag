"""执行向量化与向量写入的 IO worker。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from comet_rag.tasks import LANE_IO
from comet_rag.workers.base import WorkerProfile, build_settings

PROFILE = WorkerProfile(
    name="embedder",
    lane=LANE_IO,
    # 单进程高并发。上限由模型服务扛得住多少来定，不是由本机 CPU 定。
    max_jobs=32,
    scaling="调大 max_jobs / max_concurrency；**不要**加进程（会打爆模型服务）",
    # 一份大文档要分批向量化并写库，给足时间；真卡住了有 sweep_stale 兜底
    job_timeout=3600.0,
)


def __getattr__(name: str) -> Any:
    """惰性构造 `WorkerSettings`。理由见 `preprocessor.py` 的同名函数。"""
    if name == "WorkerSettings":
        return build_settings(PROFILE)
    raise AttributeError(name)


if TYPE_CHECKING:  # 只为静态检查器声明；运行时仍由上面的 __getattr__ 惰性构造
    WorkerSettings: type


__all__ = ["PROFILE", "WorkerSettings"]
