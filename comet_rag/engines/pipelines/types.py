from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


@dataclass
class Chunk:
    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None


@dataclass
class PipelineResult:
    source_id: str
    file_type: str
    chunks: list[Chunk] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class DocxConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    heading_numbers: bool = Field(
        default=False, description="Include heading numbers in headings"
    )
    include_images: bool = Field(
        default=True, description="Include images in the document"
    )
    include_headers_footers: bool = Field(
        default=False, description="Include headers and footers in the document"
    )
    vision_model: Any = Field(
        default=None,
        description="Vision model for describing images (must implement VisionModel protocol)",
    )
    max_archive_members: int = Field(
        default=10_000, gt=0, description="DOCX ZIP 容器允许的最大成员数"
    )
    max_archive_member_bytes: int = Field(
        default=64 * 1024 * 1024,
        gt=0,
        description="DOCX 单个 ZIP 成员允许的最大解压后字节数",
    )
    max_archive_uncompressed_bytes: int = Field(
        default=256 * 1024 * 1024,
        gt=0,
        description="DOCX ZIP 容器允许的总解压后字节数",
    )
    max_archive_compression_ratio: float = Field(
        default=100.0,
        gt=0,
        description="DOCX 单成员及整体允许的最大压缩比",
    )
    max_archive_xml_elements: int = Field(
        default=2_000_000,
        gt=0,
        description="DOCX 所有 XML 成员允许的元素总数",
    )
    max_archive_xml_text_chars: int = Field(
        default=8 * 1024 * 1024,
        gt=0,
        description="DOCX 单个 XML 文本节点允许的最大字符数",
    )


class PipelineConfig(BaseModel):
    chunk_size: int = Field(default=2000, gt=0)
    chunk_overlap: int = Field(default=200, ge=0)
    embed: bool = False
    max_concurrency: int = Field(
        default=8, gt=0, description="调用模型服务的并发上限（同时在飞的请求数）"
    )
    embed_batch_size: int = Field(
        default=32,
        gt=0,
        description=(
            "流式模式下每次并发处理多少个 chunk，即**产出粒度**。"
            "首个 chunk 在第一个窗口完成后即可产出，因此该值越小首字延迟越低、"
            "整体吞吐越差；越大则相反。非流式模式下它只用来限制同时在内存中的"
            "待处理量。一个窗口会被切成几次请求，由模型声明的 batch_limit 决定"
            "（见 application/embedding_batch.py）；同时在飞几个请求由 "
            "max_concurrency 决定。三者互不相同。"
        ),
    )
    docx: DocxConfig = Field(default_factory=DocxConfig)
