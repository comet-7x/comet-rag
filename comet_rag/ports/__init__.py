"""契约与它们的词汇表：全项目的零依赖地基。

## 这里只有契约

Port 是 Protocol，不是基类。实现住在 `comet_rag.infrastructure.providers`；
适配器继承那边的基类是为了复用实现，不是为了满足契约 —— 形状对得上就算实现。

## 为什么在顶层，而不是某一层的子包

`ports/` 不 import 本项目的任何其他包，所以**谁依赖它都是向下**。这一点很
重要：`engines/`（"库"那一半）需要说出"我要一个 embedding 模型"，如果契约
住在 services 或 application 之类的上层包里，engines 就得反向依赖上层 ——
那正是本项目此前踩过的一次（见 architecture.md 的依赖方向一节）。

## 值对象也在这里

`content.py` 里的 `MediaResource` / `ContentInput` / `RerankDocument` 等是
Port 签名里出现的类型，也就是这套契约的**词汇表**。词汇表和契约分居两个
顶层包（原先的 `comet_rag/models/`）没有带来任何好处，只让"models"这个词
在仓库里多了一种含义。
"""

from .content import (
    ContentInput,
    ContentPart,
    ImageContent,
    MediaResource,
    RankedDocument,
    RerankDocument,
    TextContent,
)
from .embedding import (
    EmbeddingPort,
    EmbeddingTask,
    MultimodalEmbeddingPort,
)
from .gate import AsyncGate
from .reranker import RerankerPort

__all__ = [
    "AsyncGate",
    "ContentInput",
    "ContentPart",
    "EmbeddingPort",
    "EmbeddingTask",
    "ImageContent",
    "MediaResource",
    "MultimodalEmbeddingPort",
    "RankedDocument",
    "RerankDocument",
    "RerankerPort",
    "TextContent",
]
