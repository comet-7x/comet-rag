"""跨层契约及其值对象。"""

from .content import (
    ContentInput,
    ContentPart,
    ImageContent,
    MediaResource,
    RankedDocument,
    RerankDocument,
    TextContent,
)
from .document import (
    DocumentExtractionError,
    DocumentExtractorPort,
    DocumentProtocolError,
    DocumentResourceLimitExceeded,
    DocumentUpstreamError,
    ExtractedDocument,
    RetryableDocumentUpstreamError,
)
from .embedding import (
    EmbeddingPort,
    EmbeddingTask,
    MultimodalEmbeddingPort,
)
from .gate import AsyncGate
from .reranker import RerankerPort
from .source import LoadedResource, SourceContent, SourceLoaderPort
from .vector_store import (
    BaseVectorStore,
    CollectionNotFound,
    CollectionSchemaMismatch,
    DimensionMismatch,
    Filter,
    SearchHit,
    VectorRecord,
    VectorSearchPort,
    VectorStoreError,
    matches_filter,
)

__all__ = [
    "AsyncGate",
    "BaseVectorStore",
    "CollectionNotFound",
    "CollectionSchemaMismatch",
    "ContentInput",
    "ContentPart",
    "DocumentExtractionError",
    "DocumentExtractorPort",
    "DocumentProtocolError",
    "DocumentResourceLimitExceeded",
    "DocumentUpstreamError",
    "DimensionMismatch",
    "EmbeddingPort",
    "EmbeddingTask",
    "ExtractedDocument",
    "Filter",
    "ImageContent",
    "LoadedResource",
    "MediaResource",
    "MultimodalEmbeddingPort",
    "RankedDocument",
    "RerankDocument",
    "RerankerPort",
    "RetryableDocumentUpstreamError",
    "SourceContent",
    "SourceLoaderPort",
    "SearchHit",
    "TextContent",
    "VectorRecord",
    "VectorSearchPort",
    "VectorStoreError",
    "matches_filter",
]
