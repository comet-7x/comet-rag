"""兼容导入；模型契约已迁移到 application ports。"""

from comet_rag.application.ports.reranker import BaseReranker, RerankerPort

__all__ = ["BaseReranker", "RerankerPort"]
