from __future__ import annotations

import pytest

from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.ports import VectorRecord


class UncopyableMetadata:
    def __deepcopy__(self, memo: dict[int, object]) -> object:
        raise RuntimeError("metadata copy failed")


async def test_snapshot_does_not_expose_stored_metadata() -> None:
    store = AsyncInMemoryVectorStore()
    await store.ensure_collection("kb", dim=2)
    await store.upsert(
        "kb",
        [VectorRecord("a", "ownership", [1.0, 0.0], {"tags": ["original"]})],
    )

    snapshot = store.snapshot()
    snapshot["kb"][0]["metadata"]["tags"].append("changed")

    assert store.snapshot()["kb"][0]["metadata"] == {"tags": ["original"]}
    assert (await store.search("kb", [1.0, 0.0]))[0].metadata == {"tags": ["original"]}
    assert (await store.search_keywords("kb", "ownership"))[0].metadata == {
        "tags": ["original"]
    }


async def test_copy_failure_does_not_partially_insert_or_overwrite() -> None:
    store = AsyncInMemoryVectorStore()
    await store.ensure_collection("kb", dim=2)
    await store.upsert(
        "kb", [VectorRecord("existing", "original", [1.0, 0.0], {"tags": ["old"]})]
    )

    with pytest.raises(RuntimeError, match="metadata copy failed"):
        await store.upsert(
            "kb",
            [
                VectorRecord("existing", "replacement", [0.0, 1.0]),
                VectorRecord("new", "new", [1.0, 0.0]),
                VectorRecord("bad", "bad", [1.0, 0.0], {"fault": UncopyableMetadata()}),
            ],
        )

    hits = await store.search("kb", [1.0, 0.0])
    assert [(hit.id, hit.text, hit.score, hit.metadata) for hit in hits] == [
        ("existing", "original", 1.0, {"tags": ["old"]})
    ]
    assert await store.count("kb") == 1
    assert await store.upsert("kb", [VectorRecord("new", "retry", [1.0, 0.0])]) == [
        "new"
    ]
