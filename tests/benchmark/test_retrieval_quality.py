from __future__ import annotations

import json

import pytest

from comet_rag.services.retrieval import ReturnLevel, SearchMode
from evals.retrieval.baseline import collect
from evals.retrieval.data import DATA, generate
from evals.retrieval.metrics import score

pytestmark = pytest.mark.benchmark


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    return generate(tmp_path_factory.mktemp("quality") / "corpus")


def test_generated_bytes_and_manifest_are_repeatable(dataset, tmp_path) -> None:
    first = generate(tmp_path / "first")
    second = generate(tmp_path / "second")
    assert first.fingerprint == second.fingerprint == dataset.fingerprint
    for path in (tmp_path / "first").iterdir():
        assert path.read_bytes() == (tmp_path / "second" / path.name).read_bytes()
    with pytest.raises(FileExistsError):
        generate(tmp_path / "first")
    assert len(dataset.corpus.documents) == 7
    assert len(dataset.judgments.queries) == 18
    assert len(dataset.judgments.evidence) == 14
    assert "CPU | 2 | 增加进程" in dataset.documents["workers"].markdown
    assert "03:10" in dataset.documents["star"].markdown


@pytest.mark.parametrize("mode", list(SearchMode))
@pytest.mark.parametrize(
    "strategy,level",
    [
        ("recursive", ReturnLevel.CHILD),
        ("markdown", ReturnLevel.CHILD),
        ("hierarchy", ReturnLevel.CHILD),
        ("hierarchy", ReturnLevel.PARENT),
    ],
)
async def test_fixed_corpus_regression(dataset, mode, strategy, level) -> None:
    run = await collect(dataset, strategy=strategy, mode=mode, level=level)
    actual = score(dataset, run)
    expected = json.loads((DATA / "offline_baseline.json").read_text())[
        f"{strategy}-{level.value}-{mode.value}"
    ]
    assert actual["dataset_sha256"] == expected["dataset_sha256"]
    assert actual["queries"] == 18
    assert actual["positive_queries"] == 16
    assert actual["no_answer_queries"] == 2
    assert actual["macro"] == pytest.approx(expected["macro"], abs=1e-12)
    assert actual["no_answer_return_rate@5"] == 1.0
    assert actual["mean_returned_chars@5"] == pytest.approx(
        expected["mean_returned_chars@5"]
    )
    assert run.settings["indexed_children"] == expected["settings"]["indexed_children"]
    assert run.settings["stored_parents"] == expected["settings"]["stored_parents"]
    # 不允许用“关闭旧版本/跨 KB 干扰”来制造漂亮分数；它们仍物理存在于索引。
    assert any(not s.active for s in dataset.corpus.documents)
    assert {s.kb_id for s in dataset.corpus.documents} == {"ops", "lab"}


@pytest.mark.parametrize("failure", [False, True])
async def test_real_collector_owns_clients_and_never_builds_persistent_stores(
    tmp_path, monkeypatch, failure
) -> None:
    from comet_rag.composition import bootstrap
    from comet_rag.infrastructure.models.reranker.base import AsyncBaseReranker
    from evals.retrieval import real
    from evals.retrieval.baseline import AsyncHashEmbedding

    closed: list[str] = []

    class Model(AsyncHashEmbedding):
        async def _embed(self, data, **kwargs):
            if failure:
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
        raise AssertionError("质量评测不得创建持久化存储或完整应用 Context")

    for name in (
        "build_context",
        "build_database",
        "build_vector_store",
        "build_task_store",
        "build_task_executor",
        "build_document_store",
    ):
        monkeypatch.setattr(bootstrap, name, forbidden)
    monkeypatch.setattr(real, "build_embedding_model", lambda config: Model())
    monkeypatch.setattr(real, "build_reranker", lambda config: Reranker())
    config = tmp_path / "models.yaml"
    config.write_text("""server_config:
  host: 127.0.0.1
  port: 8000
infrastructure_config:
  embedding_model:
    base_url: http://unused
    model_name: test-model
    dim: 256
  reranker:
    base_url: http://unused
    model_name: test-reranker
""")
    output = tmp_path / "result"
    if failure:
        with pytest.raises(ConnectionError, match="injected model failure"):
            await real.evaluate_models(config, output, model_revision="test")
        assert not (output / "summary.json").exists()
    else:
        await real.evaluate_models(config, output, model_revision="test")
        summaries = json.loads((output / "summary.json").read_text())
        assert len(summaries) == 6
        assert {v["settings"]["rerank"] for v in summaries.values()} == {False, True}
        assert all(v["settings"]["model"] == "test-model" for v in summaries.values())
    assert closed == ["reranker", "embedding"]
