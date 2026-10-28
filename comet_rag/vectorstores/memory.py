from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence

from comet_rag.embedder.types import EmbeddingResult, EmbeddingSpace
from comet_rag.vectorstores.base import _binding, _hit, _ids, _limit, _query, _records
from comet_rag.vectorstores.filters import (
    Filter,
    _field_types,
    matches,
    validate_filter,
)
from comet_rag.vectorstores.types import (
    RecordVerification,
    VectorConfirmation,
    VectorHit,
    VectorRecord,
)


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def _score(left: tuple[float, ...], right: tuple[float, ...], metric: str) -> float:
    if metric == "L2":
        return math.fsum((a - b) ** 2 for a, b in zip(left, right, strict=True))
    if metric == "IP":
        return math.fsum(a * b for a, b in zip(left, right, strict=True))
    left_scale, right_scale = max(map(abs, left)), max(map(abs, right))
    if not left_scale or not right_scale:
        return 0.0
    a_values = tuple(value / left_scale for value in left)
    b_values = tuple(value / right_scale for value in right)
    dot = math.fsum(a * b for a, b in zip(a_values, b_values, strict=True))
    return dot / math.sqrt(
        math.fsum(a * a for a in a_values) * math.fsum(b * b for b in b_values)
    )


class AsyncMemoryVectorStore:
    """单 scope 精确搜索；写入整批验证后才替换内存快照。"""

    def __init__(
        self,
        *,
        index_scope: str,
        index_profile_id: str,
        space: EmbeddingSpace,
        field_types: Mapping[str, type] | None = None,
    ) -> None:
        _binding(index_scope, index_profile_id, space)
        self._index_scope = index_scope
        self._index_profile_id = index_profile_id
        self._space = space
        self._fields = _field_types(field_types)
        self._records: dict[str, VectorRecord] = {}

    @property
    def index_scope(self) -> str:
        return self._index_scope

    @property
    def index_profile_id(self) -> str:
        return self._index_profile_id

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    async def prepare(self) -> None:
        return None

    async def upsert(self, records: Sequence[VectorRecord]) -> VectorConfirmation:
        snapshots = _records(
            records, self.index_scope, self.index_profile_id, self.space, self._fields
        )
        for record in snapshots:
            previous = self._records.get(record.record_id)
            if previous is not None and previous.fingerprint != record.fingerprint:
                raise ValueError("conflicting content for the same record ID")
        self._records = self._records | {
            record.record_id: record for record in snapshots
        }
        return VectorConfirmation(
            tuple(record.record_id for record in snapshots),
            accepted=True,
            query_visible=True,
            guarantee="memory; volatile",
        )

    async def search(
        self, query: EmbeddingResult, *, limit: int = 40, filters: Filter | None = None
    ) -> tuple[VectorHit, ...]:
        _limit(limit)
        _query(query, self.space)
        validate_filter(filters, self._fields)
        hits = [
            _hit(
                record,
                _score(query.vector, record.vector, self.space.metric),
                self.space.metric,
            )
            for record in self._records.values()
            if matches(record, filters)
        ]
        hits.sort(
            key=lambda hit: (
                (-hit.score if hit.higher_is_better else hit.score),
                hit.record_id,
            )
        )
        return tuple(hits[:limit])

    async def search_text(
        self, text: str, *, limit: int = 40, filters: Filter | None = None
    ) -> tuple[VectorHit, ...]:
        _limit(limit)
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        validate_filter(filters, self._fields)
        terms = set(_tokens(text))
        if not terms:
            return ()
        documents = [
            (record, Counter(_tokens(record.keyword_text)))
            for record in self._records.values()
        ]
        if not documents:
            return ()
        average_length = sum(sum(tokens.values()) for _, tokens in documents) / len(
            documents
        )
        frequencies = {
            term: sum(term in tokens for _, tokens in documents) for term in terms
        }
        hits: list[VectorHit] = []
        for record, tokens in documents:
            if not matches(record, filters):
                continue
            length = sum(tokens.values())
            score = 0.0
            for term in terms:
                count = tokens[term]
                if count:
                    inverse_frequency = math.log(
                        1
                        + (len(documents) - frequencies[term] + 0.5)
                        / (frequencies[term] + 0.5)
                    )
                    score += (
                        inverse_frequency
                        * count
                        * 2.2
                        / (count + 1.2 * (0.25 + 0.75 * length / average_length))
                    )
            if score > 0:
                hits.append(_hit(record, score, "BM25"))
        hits.sort(key=lambda hit: (-hit.score, hit.record_id))
        return tuple(hits[:limit])

    async def verify_records(
        self,
        record_ids: Sequence[str],
        *,
        fingerprints: Mapping[str, str] | None = None,
    ) -> RecordVerification:
        ids = _ids(record_ids, fingerprints)
        present, missing, unknown = [], [], []
        for record_id in ids:
            record = self._records.get(record_id)
            if record is None:
                missing.append(record_id)
            elif (
                fingerprints is not None
                and record.fingerprint != fingerprints[record_id]
            ):
                unknown.append(record_id)
            else:
                present.append(record_id)
        return RecordVerification(
            tuple(present), tuple(missing), tuple(unknown), "memory; volatile"
        )

    async def delete(self, record_ids: Sequence[str]) -> VectorConfirmation:
        ids = _ids(record_ids)
        deleting = set(ids)
        self._records = {
            key: record for key, record in self._records.items() if key not in deleting
        }
        return VectorConfirmation(
            ids, accepted=True, query_visible=True, guarantee="memory; volatile absence"
        )


__all__ = ["AsyncMemoryVectorStore"]
