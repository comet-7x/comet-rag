"""worker 进程入口。**按负载特征分，不按业务名词分**。

    uv run arq comet_rag.workers.preprocessor.WorkerSettings   # CPU 密集，加进程扩
    uv run arq comet_rag.workers.embedder.WorkerSettings       # IO 密集，加并发扩

两者都必须起：一条流水线会在 `chunking` 与 `indexing` 之间从 cpu 道移交到
io 道（见 `services/ingestion.py:_build_flow`）。**只起一个的话，任务会静静
停在另一条队列上**，状态是 PENDING，不报任何错 —— 排查时先确认两个都在跑。

单进程部署（`backends.task_executor=inprocess`）不需要 worker：分道信息被
忽略，整条流水线在 API 进程里跑完。

具体的并发默认值与扩容方式差异见 `base.py` 顶部与两个模块各自的文档。
"""

from comet_rag.workers.base import WorkerProfile, build_settings

__all__ = ["WorkerProfile", "build_settings"]
