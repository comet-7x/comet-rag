"""执行取源、解析、清洗与分块的 CPU worker。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from comet_rag.tasks import LANE_CPU
from comet_rag.workers.base import WorkerProfile, build_settings

PROFILE = WorkerProfile(
    name="preprocessor",
    lane=LANE_CPU,
    # 2 而非 1：留一个槽位，让一个任务在等磁盘/网络取源时另一个能占住 CPU。
    # 再大就纯属让任务互相拖慢了。
    max_jobs=2,
    scaling="加进程（副本数 ≈ CPU 核数）；**不要**调大 max_jobs",
    # 解析一份大 docx 可能几分钟，但超过半小时基本就是卡死了
    job_timeout=1800.0,
)


def __getattr__(name: str) -> Any:
    """惰性构造 `WorkerSettings`（PEP 562，与 `api/main.py:app` 同一套理由）。

    写成模块级常量的话，"import 这个模块"就等价于"必须存在一份合法配置"，
    `test_importable` 与任何静态检查都会被一份缺字段的 config.yaml 拦住。
    arq 的 CLI 用 `getattr(module, "WorkerSettings")` 取它，所以这里能接上。
    """
    if name == "WorkerSettings":
        return build_settings(PROFILE)
    raise AttributeError(name)


if TYPE_CHECKING:  # 只为静态检查器声明；运行时仍由上面的 __getattr__ 惰性构造
    WorkerSettings: type


__all__ = ["PROFILE", "WorkerSettings"]
