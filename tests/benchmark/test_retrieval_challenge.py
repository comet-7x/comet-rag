from __future__ import annotations

import json
import subprocess
import sys

import pytest

from comet_rag.infrastructure.persistence.vector_store import AsyncInMemoryVectorStore
from comet_rag.services.retrieval import SearchMode
from evals.retrieval.baseline import collect
from evals.retrieval.data import DATA, generate
from evals.retrieval.metrics import score

pytestmark = pytest.mark.benchmark
CHALLENGE = DATA / "challenge"


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    return generate(tmp_path_factory.mktemp("challenge") / "corpus", data=CHALLENGE)


def test_challenge_keeps_long_tail_and_title_facts(dataset) -> None:
    assert len(dataset.corpus.documents) == 11
    assert len(dataset.judgments.queries) == 31
    assert len(dataset.judgments.evidence) == 26
    text = dataset.documents["star_long"].markdown
    assert len(text) > 12000
    assert dataset.spans["star_long_checksum"][0] > len(text) * 0.99
    assert "紧急维护电话为 6118" in dataset.documents["heading_fact"].markdown
    assert "紧急授权码为 5931" in dataset.documents["leaf_fact"].markdown
    assert "DBQ-72 表示快照校验失败" in dataset.documents["heading_only"].markdown
    middle = next(
        b for b in dataset.documents["paths"].blocks if "中层处理" in b.heading_path
    )
    assert middle.heading_path == ("层级处理手册", "部署", "中层处理")
    query = next(q for q in dataset.judgments.queries if q.id == "star_long_rto")
    assert query.relevance == {"star_long": 2}
    assert sum(q.category == "no_answer" for q in dataset.judgments.queries) == 6


def test_cli_selects_the_requested_corpus(tmp_path) -> None:
    output = tmp_path / "cli-corpus"
    subprocess.run(  # noqa: S603 — 固定解释器与参数，不经过 shell
        [
            sys.executable,
            "-m",
            "evals.retrieval",
            "generate",
            "--data",
            str(CHALLENGE),
            "--out",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    manifest = json.loads((output / "manifest.json").read_text())
    assert (
        manifest["sha256"]
        == "76d06b536676b174e892ce162bce33713638c4b75d1cb5979d563de68dbf9557"
    )
    assert len(manifest["judgments"]["queries"]) == 31


@pytest.mark.parametrize("mode", list(SearchMode))
async def test_challenge_offline_regression(dataset, mode) -> None:
    run = await collect(dataset, strategy="hierarchy", mode=mode)
    report = score(dataset, run)
    expected = json.loads((CHALLENGE / "offline_baseline.json").read_text())[mode.value]
    assert report["dataset_sha256"] == expected["dataset_sha256"]
    assert report["positive_queries"] == 25
    assert run.settings["indexed_children"] == 315
    children, fetch = run.settings["indexed_children"], run.settings["fetch_k"]
    assert type(children) is int and type(fetch) is int
    assert children > fetch
    assert report["macro"] == pytest.approx(expected["macro"], abs=1e-12)


async def test_injected_evaluation_store_needs_identity_and_stays_caller_owned(
    dataset,
) -> None:
    class ObservedStore(AsyncInMemoryVectorStore):
        closed = False

        async def close(self) -> None:
            self.closed = True
            await super().close()

    store = ObservedStore()
    try:
        with pytest.raises(ValueError, match="声明后端身份"):
            await collect(
                dataset,
                strategy="hierarchy",
                mode=SearchMode.KEYWORD,
                vector_store=store,
            )
        run = await collect(
            dataset,
            strategy="hierarchy",
            mode=SearchMode.KEYWORD,
            vector_store=store,
            backend_name="injected reference",
        )
        assert run.settings["backend"] == "injected reference"
        assert await store.count("ops") > 50
        assert not store.closed
    finally:
        await store.close()
