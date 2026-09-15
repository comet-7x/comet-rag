from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard

from comet_rag.engines.chunkers import ChunkDraft
from comet_rag.engines.pipelines import HookProvider, PipelineConfig, PipelineHooks
from comet_rag.engines.utils import compute_sha256
from comet_rag.ports import (
    DocumentBlock,
    DocumentResourceLimitExceeded,
    NormalizedDocument,
)

# 这里是文档分块与下游物化的唯一用例边界，避免 Pipeline 与任务链路各自拼装。

# 文档和请求 metadata 不能伪造结构事实；这些字段只接受 ChunkingStrategy 的输出。
_CHUNK_FACT_METADATA_KEYS = frozenset(
    {
        "block_kind",
        "heading_path",
        "page_number",
    }
)

# 来源身份、位置和索引关系由物化阶段生成，任何上游层都不能覆盖。
_SYSTEM_CHUNK_METADATA_KEYS = frozenset(
    {
        "chunk_end",
        "chunk_index",
        "chunk_start",
        "document_revision",
        "file_type",
        "kb_id",
        "parent_id",
        "source",
        "source_id",
        "total_chunks",
    }
)


@dataclass(frozen=True, slots=True)
class MaterializedChunk:
    """ChunkDraft 加上来源身份后的下游共享形态。"""

    id: str
    text: str
    ordinal: int
    metadata: dict[str, object]


class ChunkingService:
    """选择文档级策略，并在跨层前校验 ChunkDraft 契约。"""

    def __init__(
        self,
        config: PipelineConfig,
        hooks: HookProvider | None = None,
    ) -> None:
        self._config = config
        self._hooks = hooks or PipelineHooks

    def split(
        self, file_type: str, document: NormalizedDocument, /
    ) -> list[ChunkDraft]:
        drafts = self._hooks.get_document_chunker(file_type.lower())(
            document, self._config
        )
        if not isinstance(drafts, list):
            raise TypeError("document_chunker 必须返回 list[ChunkDraft]")
        _validate_drafts(drafts, document=document)
        return drafts


def _merge_chunk_metadata(
    *,
    document: Mapping[str, object] | None = None,
    request: Mapping[str, object] | None = None,
    chunk: Mapping[str, object] | None = None,
    system: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """按文档 < 请求 < 块级事实 < 系统字段的顺序合并 metadata。"""

    merged: dict[str, object] = {}
    protected = _SYSTEM_CHUNK_METADATA_KEYS | _CHUNK_FACT_METADATA_KEYS
    for layer in (document, request):
        if layer:
            merged.update(
                (key, value)
                for key, value in layer.items()
                if key not in protected
            )
    if chunk:
        merged.update(
            (key, value)
            for key, value in chunk.items()
            if key not in _SYSTEM_CHUNK_METADATA_KEYS
        )
    if system:
        merged.update(system)
    return merged


def dump_chunk_drafts(
    drafts: Sequence[ChunkDraft],
    /,
    *,
    max_bytes: int | None = None,
) -> list[dict[str, object]]:
    """转为 Task context 可安全跨进程持久化的窄结构。"""
    _validate_drafts(drafts)
    payload = [
        {
            "text": draft.text,
            "ordinal": draft.ordinal,
            "start_char": draft.start_char,
            "end_char": draft.end_char,
            "metadata": dict(draft.metadata),
        }
        for draft in drafts
    ]
    _validate_json_payload(payload, name="ChunkDraft", max_bytes=max_bytes)
    return payload


def dump_document_blocks(
    blocks: Sequence[DocumentBlock],
    /,
    *,
    max_bytes: int | None = None,
) -> list[dict[str, object]]:
    """保存结构事实，确保跨 worker 后仍能选择同一种文档级策略。"""
    payload: list[dict[str, object]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, DocumentBlock):
            raise TypeError(
                f"DocumentBlock Task payload 第 {index} 项必须是 DocumentBlock"
            )
        payload.append(
            {
                "kind": block.kind,
                "ordinal": block.ordinal,
                "start_char": block.start_char,
                "end_char": block.end_char,
                "heading_path": list(block.heading_path),
                "page_number": block.page_number,
                "metadata": dict(block.metadata),
            }
        )
    _validate_json_payload(payload, name="DocumentBlock", max_bytes=max_bytes)
    return payload


def load_document_blocks(payload: object, /) -> tuple[DocumentBlock, ...]:
    """从 Task context 严格恢复文档结构事实。"""
    if not isinstance(payload, list):
        raise TypeError("DocumentBlock Task payload 必须是 list")
    allowed_keys = {
        "kind",
        "ordinal",
        "start_char",
        "end_char",
        "heading_path",
        "page_number",
        "metadata",
    }
    blocks: list[DocumentBlock] = []
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise TypeError(f"DocumentBlock Task payload 第 {index} 项必须是 object")
        unknown_keys = [key for key in item if key not in allowed_keys]
        if unknown_keys:
            raise ValueError(
                f"DocumentBlock Task payload 第 {index} 项包含未知字段："
                f"{unknown_keys!r}"
            )
        heading_path = item.get("heading_path", [])
        metadata = item.get("metadata", {})
        kind = item.get("kind")
        ordinal = item.get("ordinal")
        start_char = item.get("start_char")
        end_char = item.get("end_char")
        page_number = item.get("page_number")
        if not isinstance(kind, str):
            raise TypeError(
                f"DocumentBlock Task payload 第 {index} 项 kind 必须是 str"
            )
        if not _is_int(ordinal):
            raise TypeError(
                f"DocumentBlock Task payload 第 {index} 项 ordinal 必须是 int"
            )
        if not _is_int(start_char):
            raise TypeError(
                f"DocumentBlock Task payload 第 {index} 项 start_char 必须是 int"
            )
        if not _is_int(end_char):
            raise TypeError(
                f"DocumentBlock Task payload 第 {index} 项 end_char 必须是 int"
            )
        if page_number is not None and not _is_int(page_number):
            raise TypeError(
                f"DocumentBlock Task payload 第 {index} 项 page_number "
                "必须是 int | None"
            )
        if not isinstance(heading_path, list) or any(
            not isinstance(heading, str) for heading in heading_path
        ):
            raise TypeError(
                f"DocumentBlock Task payload 第 {index} 项 heading_path "
                "必须是 list[str]"
            )
        if not isinstance(metadata, Mapping):
            raise TypeError(
                f"DocumentBlock Task payload 第 {index} 项 metadata 必须是 object"
            )
        blocks.append(
            DocumentBlock(
                kind=kind,
                ordinal=ordinal,
                start_char=start_char,
                end_char=end_char,
                heading_path=tuple(heading_path),
                page_number=page_number,
                metadata=dict(metadata),
            )
        )
    return tuple(blocks)


def load_chunk_drafts(payload: object, /) -> list[ChunkDraft]:
    """从 Task context 恢复 ChunkDraft，拒绝宽松的隐式类型转换。"""
    if not isinstance(payload, list):
        raise TypeError("ChunkDraft Task payload 必须是 list")

    drafts: list[ChunkDraft] = []
    allowed_keys = {"text", "ordinal", "start_char", "end_char", "metadata"}
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise TypeError(f"ChunkDraft Task payload 第 {index} 项必须是 object")
        unknown_keys = [key for key in item if key not in allowed_keys]
        if unknown_keys:
            raise ValueError(
                f"ChunkDraft Task payload 第 {index} 项包含未知字段：{unknown_keys!r}"
            )
        text = item.get("text")
        ordinal = item.get("ordinal")
        start_char = item.get("start_char")
        end_char = item.get("end_char")
        metadata = item.get("metadata", {})
        if not isinstance(text, str):
            raise TypeError(f"ChunkDraft Task payload 第 {index} 项 text 必须是 str")
        if not _is_int(ordinal):
            raise TypeError(f"ChunkDraft Task payload 第 {index} 项 ordinal 必须是 int")
        if start_char is not None and not _is_int(start_char):
            raise TypeError(
                f"ChunkDraft Task payload 第 {index} 项 start_char 必须是 int | None"
            )
        if end_char is not None and not _is_int(end_char):
            raise TypeError(
                f"ChunkDraft Task payload 第 {index} 项 end_char 必须是 int | None"
            )
        if not isinstance(metadata, Mapping):
            raise TypeError(
                f"ChunkDraft Task payload 第 {index} 项 metadata 必须是 object"
            )
        drafts.append(
            ChunkDraft(
                text=text,
                ordinal=ordinal,
                start_char=start_char,
                end_char=end_char,
                metadata=dict(metadata),
            )
        )
    _validate_drafts(drafts)
    return drafts


def _validate_json_payload(
    payload: object,
    /,
    *,
    name: str,
    max_bytes: int | None,
) -> None:
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("max_bytes 必须大于 0")
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} Task payload 必须可 JSON 序列化：{exc}") from exc
    if max_bytes is not None and len(encoded) > max_bytes:
        raise DocumentResourceLimitExceeded(
            f"{name} Task payload 超过限制：{len(encoded)} > {max_bytes} bytes"
        )


def materialize_chunk_drafts(
    drafts: Sequence[ChunkDraft],
    /,
    *,
    source_id: str,
    source: str,
    file_type: str,
    document_metadata: Mapping[str, object] | None = None,
    request_metadata: Mapping[str, object] | None = None,
    kb_id: str | None = None,
) -> list[MaterializedChunk]:
    """集中生成 ID 与 metadata，Pipeline 和 Task 不再各拼一份。"""
    _validate_drafts(drafts)
    total = len(drafts)
    materialized: list[MaterializedChunk] = []
    for draft in drafts:
        system: dict[str, object] = {
            "source": source,
            "source_id": source_id,
            "file_type": file_type,
            "total_chunks": total,
            "chunk_index": draft.ordinal,
        }
        if kb_id is not None:
            system["kb_id"] = kb_id
        if draft.start_char is not None and draft.end_char is not None:
            system["chunk_start"] = draft.start_char
            system["chunk_end"] = draft.end_char
        materialized.append(
            MaterializedChunk(
                id=chunk_id(source_id, draft.ordinal),
                text=draft.text,
                ordinal=draft.ordinal,
                metadata=_merge_chunk_metadata(
                    document=document_metadata,
                    request=request_metadata,
                    chunk=draft.metadata,
                    system=system,
                ),
            )
        )
    return materialized


def chunk_id(source_id: str, ordinal: int, /) -> str:
    """生成平坦块稳定 ID；写入与旧尾回收必须共用这一处。"""
    if not source_id:
        raise ValueError("source_id 不能为空")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise TypeError("ordinal 必须是 int")
    if ordinal < 0:
        raise ValueError("ordinal 必须大于等于 0")
    return compute_sha256(f"{source_id}:{ordinal}")


def _validate_drafts(
    drafts: Sequence[ChunkDraft],
    *,
    document: NormalizedDocument | None = None,
) -> None:
    for expected, draft in enumerate(drafts):
        if not isinstance(draft, ChunkDraft):
            raise TypeError(
                f"document_chunker 第 {expected} 项必须是 ChunkDraft，"
                f"收到 {type(draft).__name__}"
            )
        if draft.ordinal != expected:
            raise ValueError(
                "ChunkDraft.ordinal 必须从 0 严格连续："
                f"第 {expected} 项收到 {draft.ordinal}"
            )
        if document is None or draft.start_char is None or draft.end_char is None:
            continue
        if draft.end_char > len(document.markdown):
            raise ValueError(
                f"ChunkDraft[{expected}] span 超出 NormalizedDocument.markdown"
            )
        if document.markdown[draft.start_char : draft.end_char] != draft.text:
            raise ValueError(
                f"ChunkDraft[{expected}] text 与 NormalizedDocument span 不一致"
            )


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = [
    "ChunkingService",
    "MaterializedChunk",
    "chunk_id",
    "dump_chunk_drafts",
    "dump_document_blocks",
    "load_chunk_drafts",
    "load_document_blocks",
    "materialize_chunk_drafts",
]
