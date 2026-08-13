"""向量化 worker：调 embedding 模型 + 写向量库（`LANE_IO`）。

    uv run arq comet_rag.workers.embedder.WorkerSettings

## 并发开到 32，而且**不要**靠加进程扩容

这里的时间几乎全花在等模型服务与 Milvus 回包上，CPU 基本闲着。一个进程内
挂几十个协程，等待彼此重叠，吞吐几乎线性上涨 —— 而且共用同一个 httpx
连接池，连接数不随并发爆炸（spec S4-4）。

**加进程反而更慢**，这是本文件最该记住的一句：
  · 对模型服务的连接数 = 进程数 × 每进程并发，很容易把它的接受队列打满；
  · 请求在服务端排队，每个 worker 只看到"上游变慢了"，**看不出是自己造成的**；
  · 服务端的动态批处理也被打散 —— 同样的请求量，batch 变小、GPU 利用率下降。

真需要更大吞吐时，先调 `max_jobs` 与 `PipelineConfig.max_concurrency`，
它们才是这条道上的旋钮。只有当**单进程的 CPU 真的打满**（序列化/JSON 解析
成了瓶颈）时，加进程才开始有意义。

## 两级并发闸门是有意的

    max_jobs                    同时在跑几个**任务**
    PipelineConfig.max_concurrency   一个任务内同时发几个**请求**

模型服务感受到的压力是两者相乘。只掐一层的话，另一层会悄悄把它翻倍 ——
S4-2 要的是可预测的上限，不是"大概不会太多"。
"""

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
