"""知识库用例：元数据与向量库的一致性维护（spec A12）。

本文件最重要的一条是"换了 embedding 模型必须拒绝"。
维度不一致还能被向量库拦住，**同维度的不同模型谁也拦不住** ——
混用不报错、不崩溃，只是检索静默劣化，且事后分不清哪些 chunk 该重算。
"""

from __future__ import annotations

import pytest

from comet_rag.infrastructure.knowledge_base import (
    EmbeddingModelChanged,
    InMemoryKnowledgeBaseRepository,
    KnowledgeBaseNotFound,
)
from comet_rag.infrastructure.vectorstore import (
    CollectionNotFound,
    InMemoryVectorStore,
    VectorRecord,
)
from comet_rag.services.knowledge_base import KnowledgeBaseService, KnowledgeBaseSpec

MODEL = "Qwen/Qwen3-VL-Embedding-8B"
DIM = 4


@pytest.fixture
def store() -> InMemoryVectorStore:
    return InMemoryVectorStore()


@pytest.fixture
def repo() -> InMemoryKnowledgeBaseRepository:
    return InMemoryKnowledgeBaseRepository()


@pytest.fixture
def service(repo, store) -> KnowledgeBaseService:
    return KnowledgeBaseService(
        repository=repo,
        vector_store=store,
        embedding_model=MODEL,
        embedding_dim=DIM,
    )


def other_model_service(repo, store, model: str) -> KnowledgeBaseService:
    return KnowledgeBaseService(
        repository=repo, vector_store=store, embedding_model=model, embedding_dim=DIM
    )


# ── 创建 ───────────────────────────────────────────────────────────────────


async def test_create_records_model_and_dim(service: KnowledgeBaseService) -> None:
    """这两个字段是这张表存在的全部理由。"""
    view = await service.create(KnowledgeBaseSpec(kb_id="kb-1"))

    assert view.embedding_model == MODEL
    assert view.embedding_dim == DIM


async def test_create_also_prepares_the_collection(
    service: KnowledgeBaseService, store: InMemoryVectorStore
) -> None:
    """元数据与 collection 必须成对存在，否则入库时会莫名报 CollectionNotFound。"""
    await service.create(KnowledgeBaseSpec(kb_id="kb-1"))

    assert await store.acount("kb-1") == 0  # 不抛 CollectionNotFound 即为已建


async def test_create_is_idempotent(service: KnowledgeBaseService) -> None:
    """客户端常在每次入库前"确保库存在"。不幂等就得让每个调用方先查后建，
    等于把竞态推给调用方。"""
    first = await service.create(KnowledgeBaseSpec(kb_id="kb-1", name="原名"))
    again = await service.create(KnowledgeBaseSpec(kb_id="kb-1", name="新名"))

    assert again.kb_id == first.kb_id
    assert again.created_at == first.created_at
    assert again.name == "原名", "幂等创建不该悄悄改掉已有属性"


async def test_name_defaults_to_kb_id(service: KnowledgeBaseService) -> None:
    view = await service.create(KnowledgeBaseSpec(kb_id="kb-1"))

    assert view.name == "kb-1"


async def test_description_is_kept(service: KnowledgeBaseService) -> None:
    view = await service.create(
        KnowledgeBaseSpec(kb_id="kb-1", description="产品文档库")
    )

    assert view.description == "产品文档库"


# ── A12：模型变更必须被拒绝 ────────────────────────────────────────────────


async def test_recreate_with_changed_model_is_rejected(repo, store) -> None:
    """**本文件最重要的一条**。

    同维度的两个不同模型产出的向量落在完全不同的语义空间里，
    混在一起检索会静默劣化 —— 没有任何报错，只是结果变差。
    """
    await other_model_service(repo, store, MODEL).create(
        KnowledgeBaseSpec(kb_id="kb-1")
    )

    with pytest.raises(EmbeddingModelChanged) as exc:
        await other_model_service(repo, store, "别的模型").create(
            KnowledgeBaseSpec(kb_id="kb-1")
        )

    assert exc.value.expected == MODEL
    assert exc.value.actual == "别的模型"


async def test_ingest_with_changed_model_is_rejected(repo, store) -> None:
    """入库前的一致性检查 —— 这是 A12 在写路径上的执行点。"""
    await other_model_service(repo, store, MODEL).create(
        KnowledgeBaseSpec(kb_id="kb-1")
    )

    with pytest.raises(EmbeddingModelChanged):
        await other_model_service(repo, store, "别的模型").resolve_for_ingest("kb-1")


async def test_ingest_with_same_model_passes(
    service: KnowledgeBaseService,
) -> None:
    await service.create(KnowledgeBaseSpec(kb_id="kb-1"))

    kb = await service.resolve_for_ingest("kb-1")

    assert kb.embedding_dim == DIM


async def test_ingest_into_unknown_kb_is_rejected(
    service: KnowledgeBaseService,
) -> None:
    """往不存在的库里灌数据必须报错，不能顺手建一个 ——
    那样打错一个字就会凭空多出一个知识库，而且没人会发现。"""
    with pytest.raises(KnowledgeBaseNotFound):
        await service.resolve_for_ingest("打错的名字")


# ── 查询 ───────────────────────────────────────────────────────────────────


async def test_chunk_count_is_live(
    service: KnowledgeBaseService, store: InMemoryVectorStore
) -> None:
    """现查而非存计数：冗余计数一定会漂移，而漂移了的计数比没有更误导人。"""
    await service.create(KnowledgeBaseSpec(kb_id="kb-1"))
    await store.aupsert(
        "kb-1",
        [VectorRecord(id="a", text="内容", embedding=[1.0, 0, 0, 0])],
    )

    assert (await service.get("kb-1")).chunk_count == 1


async def test_get_unknown_raises(service: KnowledgeBaseService) -> None:
    with pytest.raises(KnowledgeBaseNotFound):
        await service.get("从未建过")


async def test_list_returns_all(service: KnowledgeBaseService) -> None:
    for i in range(3):
        await service.create(KnowledgeBaseSpec(kb_id=f"kb-{i}"))

    assert len(await service.list()) == 3


async def test_count_failure_does_not_break_the_query(
    repo, store, service: KnowledgeBaseService
) -> None:
    """统计失败不该让整个查询失败 —— 元数据本身还是有用的。"""
    await service.create(KnowledgeBaseSpec(kb_id="kb-1"))
    await store.adrop_collection("kb-1")  # 制造元数据在、collection 没了的状态

    view = await service.get("kb-1")

    assert view.kb_id == "kb-1"
    assert view.chunk_count == -1, "统计不出来时用 -1 表示未知，而不是谎报 0"


# ── 删除 ───────────────────────────────────────────────────────────────────


async def test_delete_removes_both_metadata_and_vectors(
    service: KnowledgeBaseService, store: InMemoryVectorStore
) -> None:
    await service.create(KnowledgeBaseSpec(kb_id="kb-1"))
    await store.aupsert(
        "kb-1", [VectorRecord(id="a", text="内容", embedding=[1.0, 0, 0, 0])]
    )

    assert await service.delete("kb-1") is True

    with pytest.raises(KnowledgeBaseNotFound):
        await service.get("kb-1")
    with pytest.raises(CollectionNotFound):
        await store.acount("kb-1")


async def test_delete_unknown_is_not_an_error(
    service: KnowledgeBaseService,
) -> None:
    assert await service.delete("从未建过") is False


async def test_delete_drops_vectors_before_metadata(
    repo, store, service: KnowledgeBaseService
) -> None:
    """顺序不能反。先删元数据再删向量的话，中途失败会留下一堆**无主向量** ——
    没有元数据就没人知道它们属于谁、该不该清，只能人工翻库。
    """
    await service.create(KnowledgeBaseSpec(kb_id="kb-1"))
    order: list[str] = []

    original_drop = store.adrop_collection
    original_delete = repo.adelete

    async def traced_drop(kb_id: str) -> None:
        order.append("vectors")
        await original_drop(kb_id)

    async def traced_delete(kb_id: str) -> bool:
        order.append("metadata")
        return await original_delete(kb_id)

    store.adrop_collection = traced_drop  # type: ignore[method-assign]
    repo.adelete = traced_delete  # type: ignore[method-assign]

    await service.delete("kb-1")

    assert order == ["vectors", "metadata"]
