from __future__ import annotations

import json

import httpx
import pytest

from comet_rag.config.schemas import (
    APPConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    ModelConfig,
    ServerConfig,
)
from comet_rag.engines.chunkers import RecursiveChunker
from comet_rag.engines.chunkers.strategies import PageChunkingStrategy
from comet_rag.engines.indexing import HierarchyBuilder
from comet_rag.infrastructure.models.reranker.base import AsyncBaseReranker
from comet_rag.services.retrieval import SearchMode
from evals.pdf import real
from evals.pdf.data import load
from evals.retrieval.baseline import AsyncHashEmbedding, collect
from evals.retrieval.metrics import score

pytestmark = pytest.mark.benchmark


@pytest.fixture(scope="module")
def dataset():
    return load()


def test_real_pdf_page_and_hierarchy_never_cross_physical_pages(dataset) -> None:
    for document in dataset.documents.values():
        chunks = PageChunkingStrategy(RecursiveChunker(800, 80)).split(document)
        hierarchy = HierarchyBuilder(
            RecursiveChunker(1600), RecursiveChunker(800, 80)
        ).split(document)
        for child in chunks:
            assert child.start_char is not None and child.end_char is not None
            block = next(
                b
                for b in document.blocks
                if b.page_number == child.metadata["page_number"]
            )
            assert (
                block.start_char <= child.start_char < child.end_char <= block.end_char
            )
            assert child.text == document.markdown[child.start_char : child.end_char]
        for parent in hierarchy.parents:
            block = next(
                b
                for b in document.blocks
                if b.page_number == parent.metadata["page_number"]
            )
            assert (
                block.start_char
                <= parent.start_char
                < parent.end_char
                <= block.end_char
            )
        for child in hierarchy.children:
            parent = hierarchy.parents[child.parent_ordinal]
            assert (
                parent.start_char
                <= child.start_char
                < child.end_char
                <= parent.end_char
            )
            assert child.metadata["page_number"] == parent.metadata["page_number"]


async def test_evaluation_page_strategy_uses_requested_size_and_real_boundaries(
    dataset,
) -> None:
    run = await collect(
        dataset,
        strategy="page",
        mode=SearchMode.HYBRID,
        child_size=800,
        child_overlap=80,
        parent_size=1600,
    )
    report = score(dataset, run)
    assert report["positive_queries"] == 20 and report["no_answer_queries"] == 4
    assert run.settings["child_size"] == 800 and run.settings["child_overlap"] == 80
    for query in run.results:
        for hit in query.hits:
            blocks = dataset.documents[hit.document_id].blocks
            assert (
                sum(b.start_char <= hit.start < hit.end <= b.end_char for b in blocks)
                == 1
            )


@pytest.mark.parametrize("failure", [None, "model", "token_count", "token_change"])
async def test_pdf_real_entry_owns_clients_and_rejects_false_token_observations(
    tmp_path, monkeypatch, failure
) -> None:
    from comet_rag.composition import bootstrap

    closed = []

    class Model(AsyncHashEmbedding):
        async def _embed(self, data, **kwargs):
            if failure == "model":
                raise ConnectionError("injected model failure")
            return await super()._embed(data, **kwargs)

        async def close(self):
            closed.append("embedding")

    class Reranker(AsyncBaseReranker):
        async def _score(self, query, documents, **kwargs):
            return [float(len(documents) - i) for i in range(len(documents))]

        async def close(self):
            closed.append("reranker")

    def forbidden(*args, **kwargs):
        raise AssertionError("PDF 评测不能创建完整应用或持久化资源")

    for name in (
        "build_context",
        "build_vector_store",
        "build_database",
        "build_document_store",
        "build_task_executor",
        "build_task_store",
    ):
        monkeypatch.setattr(bootstrap, name, forbidden)
    monkeypatch.setattr(real, "build_embedding_model", lambda config: Model())
    monkeypatch.setattr(real, "build_reranker", lambda config: Reranker())
    probes = 0

    def respond(request):
        nonlocal probes
        body = json.loads(request.content)
        assert request.url.path == "/tokenize"
        if body["prompt"] == "A fixed token probe.":
            probes += 1
        token = 2 if failure == "token_change" and probes > 1 else 1
        return httpx.Response(
            200,
            json={
                "count": True if failure == "token_count" else 1,
                "tokens": [token],
                "max_model_len": 40960,
            },
        )

    original = httpx.AsyncClient
    clients = []

    def counter(**kwargs):
        client = original(**kwargs, transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    monkeypatch.setattr(real.httpx, "AsyncClient", counter)
    config = APPConfig(
        server_config=ServerConfig(host="127.0.0.1", port=0),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused/v1", model_name="test", dim=256
            ),
            reranker=ModelConfig(base_url="http://unused/v1", model_name="counter"),
        ),
    )
    output = tmp_path / "real"
    if failure:
        with pytest.raises((ValueError, ConnectionError)):
            await real.evaluate(config, output, model_revision="unknown")
        assert not (output / "summary.json").exists()
    else:
        await real.evaluate(config, output, model_revision="unknown")
        summary = json.loads((output / "summary.json").read_text())
        assert len(summary) == 24
        assert all(
            r["settings"]["scope"]
            == "PDF native-text reference snapshot, real model, exact InMemory recall"
            for r in summary.values()
        )
        assert all(
            r["settings"]["model_revision"] == "unknown" for r in summary.values()
        )
        assert all("pending" in r["settings"]["label_status"] for r in summary.values())
        assert all(
            r["latency_ms"]["samples"] == 0
            for name, r in summary.items()
            if "-body" in name
        )
    assert closed == ["reranker", "embedding"]
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize(
    "document_id,evidence_id",
    [("nist145", "location"), ("nist145", "iaas"), ("nist144", "uniformity")],
)
def test_current_800_character_children_fragment_these_complete_quotes(
    dataset, document_id, evidence_id
) -> None:
    # 标签先于分块/排名冻结；记录当前限制，未来修复需重新做同预算质量对照。
    document = dataset.documents[document_id]
    start, end = dataset.spans[evidence_id]
    pages = PageChunkingStrategy(RecursiveChunker(800, 80)).split(document)
    hierarchy = HierarchyBuilder(
        RecursiveChunker(1600), RecursiveChunker(800, 80)
    ).split(document)
    for children in (pages, hierarchy.children):
        assert not any(
            c.start_char is not None
            and c.end_char is not None
            and c.start_char <= start < end <= c.end_char
            for c in children
        )
    assert any(p.start_char <= start < end <= p.end_char for p in hierarchy.parents)
