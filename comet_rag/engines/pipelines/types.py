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


class PipelineConfig(BaseModel):
    chunk_size: int = Field(default=2000, gt=0)
    chunk_overlap: int = Field(default=200, ge=0)
    embed: bool = False
    max_concurrency: int = Field(default=8, gt=0)
    docx: DocxConfig = Field(default_factory=DocxConfig)
