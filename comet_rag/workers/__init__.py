"""按 CPU 与 IO 负载分道的 worker 入口。"""

from comet_rag.workers.base import WorkerProfile, build_settings

__all__ = ["WorkerProfile", "build_settings"]
