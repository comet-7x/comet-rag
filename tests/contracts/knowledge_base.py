"""`KnowledgeBaseRepository` 契约。

`InMemoryKnowledgeBaseRepository` 与 `PostgresKnowledgeBaseRepository` 跑同一套。
"""

from __future__ import annotations

import pytest

from comet_rag.infrastructure.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseExists,
    KnowledgeBaseNotFound,
    KnowledgeBaseRepository,
)


def make_kb(kb_id: str = "kb-1", **overrides) -> KnowledgeBase:
    defaults = {
        "kb_id": kb_id,
        "name": f"知识库 {kb_id}",
        "embedding_model": "Qwen/Qwen3-VL-Embedding-8B",
        "embedding_dim": 4096,
    }
    return KnowledgeBase(**{**defaults, **overrides})


class KnowledgeBaseRepositoryContract:
    @pytest.fixture
    async def repo(self) -> KnowledgeBaseRepository:  # pragma: no cover
        raise NotImplementedError("实现方必须提供 repo fixture")

    async def test_create_then_get(self, repo: KnowledgeBaseRepository) -> None:
        created = await repo.acreate(make_kb())

        loaded = await repo.aget(created.kb_id)

        assert loaded is not None
        assert loaded.kb_id == created.kb_id
        assert loaded.embedding_model == "Qwen/Qwen3-VL-Embedding-8B"
        assert loaded.embedding_dim == 4096

    async def test_get_unknown_returns_none(
        self, repo: KnowledgeBaseRepository
    ) -> None:
        assert await repo.aget("从未建过") is None

    async def test_require_unknown_raises(self, repo: KnowledgeBaseRepository) -> None:
        with pytest.raises(KnowledgeBaseNotFound):
            await repo.arequire("从未建过")

    async def test_duplicate_create_is_rejected(
        self, repo: KnowledgeBaseRepository
    ) -> None:
        """靠唯一约束而非"先查后插"—— 后者在并发下有竞态窗口。"""
        await repo.acreate(make_kb())

        with pytest.raises(KnowledgeBaseExists):
            await repo.acreate(make_kb())

    async def test_loaded_row_is_a_copy(self, repo: KnowledgeBaseRepository) -> None:
        await repo.acreate(make_kb())

        loaded = await repo.aget("kb-1")
        assert loaded is not None
        loaded.name = "偷偷改的"

        again = await repo.aget("kb-1")
        assert again is not None
        assert again.name != "偷偷改的"

    async def test_list_is_newest_first(self, repo: KnowledgeBaseRepository) -> None:
        from datetime import timedelta

        from comet_rag.tasks.models import Time

        base = Time.now()
        await repo.acreate(make_kb("old", created_at=base - timedelta(hours=2)))
        await repo.acreate(make_kb("new", created_at=base))

        rows = await repo.alist()

        assert [r.kb_id for r in rows][:2] == ["new", "old"]

    async def test_list_paginates(self, repo: KnowledgeBaseRepository) -> None:
        for i in range(5):
            await repo.acreate(make_kb(f"kb-{i}"))

        assert len(await repo.alist(limit=2)) == 2
        assert len(await repo.alist(limit=10, offset=5)) == 0

    async def test_delete_removes_row(self, repo: KnowledgeBaseRepository) -> None:
        await repo.acreate(make_kb())

        assert await repo.adelete("kb-1") is True
        assert await repo.aget("kb-1") is None

    async def test_delete_unknown_is_not_an_error(
        self, repo: KnowledgeBaseRepository
    ) -> None:
        assert await repo.adelete("从未建过") is False

    async def test_optional_fields_round_trip(
        self, repo: KnowledgeBaseRepository
    ) -> None:
        await repo.acreate(make_kb(description="一句说明"))
        loaded = await repo.aget("kb-1")

        assert loaded is not None
        assert loaded.description == "一句说明"
