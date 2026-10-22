from __future__ import annotations

from comet_rag.infrastructure.persistence.vector_store.memory import (
    AsyncInMemoryVectorStore,
)
from tests.contracts.retrieval_scenario import assert_retrieval_modes


async def test_real_milvus_scenario_uses_current_model_contract() -> None:
    """中间件缺席时仍执行相同编排，避免 skip 掩盖测试替身的接口过期。"""
    store = AsyncInMemoryVectorStore()
    try:
        await assert_retrieval_modes(store, store)
    finally:
        await store.close()
