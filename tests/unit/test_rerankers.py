from __future__ import annotations

import asyncio
import builtins
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from comet_rag.content import ContentItem, ImagePart, MediaRef, TextPart
from comet_rag.rerankers import AsyncReranker, RankedItem, RerankResult
from comet_rag.rerankers.models import local


class Tensor:
    def __init__(
        self, values: list[list[float]], *, shape: tuple[int, ...] | None = None
    ) -> None:
        self.values = values
        self.shape = shape if shape is not None else (len(values), 1)

    def detach(self) -> Tensor:
        return self

    def cpu(self) -> Tensor:
        return self

    def tolist(self) -> list[list[float]]:
        return self.values


class Batch(dict[str, Any]):
    def to(self, device: str) -> Batch:
        self["device"] = device
        return self


class Tokenizer:
    model_max_length = 64

    def __init__(self) -> None:
        self.pairs: list[tuple[str, str]] = []
        self.calls: list[dict[str, Any]] = []
        self.pad_batches: list[list[dict[str, Any]]] = []

    def __call__(
        self, queries: list[str], texts: list[str], **kwargs: Any
    ) -> dict[str, Any]:
        self.pairs.extend(zip(queries, texts, strict=True))
        self.calls.append(kwargs)
        ids = [
            [1] * (len(query) + len(text) + 3)
            for query, text in zip(queries, texts, strict=True)
        ]
        return {
            "input_ids": ids,
            "attention_mask": [[1] * len(tokens) for tokens in ids],
        }

    def pad(self, features: list[dict[str, Any]], **kwargs: Any) -> Batch:
        assert kwargs == {"padding": True, "return_tensors": "pt"}
        self.pad_batches.append(features)
        return Batch(input_ids=[feature["input_ids"] for feature in features])


class Model:
    def __init__(self, scores: list[float]) -> None:
        self.config = SimpleNamespace(num_labels=1, max_position_embeddings=64)
        self.scores = scores
        self.calls = 0
        self.offset = 0
        self.closed = False
        self.device: str | None = None
        self.evaluated = False

    def eval(self) -> Model:
        self.evaluated = True
        return self

    def to(self, device: str) -> Model:
        self.device = device
        return self

    def __call__(self, **inputs: Any) -> SimpleNamespace:
        assert inputs["device"] == self.device
        count = len(inputs["input_ids"])
        scores = self.scores[self.offset : self.offset + count]
        self.offset += count
        self.calls += 1
        return SimpleNamespace(logits=Tensor([[score] for score in scores]))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    def sdk(name: str) -> Any:
        assert name == "torch"
        return SimpleNamespace(inference_mode=nullcontext)

    monkeypatch.setattr(local, "import_module", sdk)


def reranker(
    model: Model | None = None, tokenizer: Tokenizer | None = None, **kwargs: Any
) -> local.AsyncLocalReranker:
    return local.AsyncLocalReranker(
        model=model if model is not None else Model([0.0]),
        tokenizer=tokenizer if tokenizer is not None else Tokenizer(),
        model_id="test-cross-encoder",
        revision="fixed-revision",
        **kwargs,
    )


def test_result_validates_ids_one_based_ranks_and_finite_scores() -> None:
    candidates = [ContentItem("a", "same"), ContentItem("b", "same")]
    result = RerankResult(
        (RankedItem("b", 1, 2), RankedItem("a", 2, 1)),
        "model",
        "logit",
        revision="revision",
    )
    result.validate(candidates)
    assert result.items[0].score == 2.0
    with pytest.raises(ValueError, match="every candidate"):
        result.validate(candidates[:1])
    with pytest.raises(ValueError, match="every candidate"):
        RerankResult((RankedItem("a", 1),), "model", "rank_only").validate(candidates)
    with pytest.raises(ValueError, match="unique IDs"):
        result.validate([candidates[0], candidates[0]])
    with pytest.raises(ValueError, match="unique IDs"):
        result.validate(["invalid"])  # type: ignore[arg-type]
    for items in ((RankedItem("a", 1), RankedItem("a", 2)), (RankedItem("a", 2),)):
        with pytest.raises(ValueError, match="unique.*one-based"):
            RerankResult(items, "model", "rank_only")
    for rank in (0, -1, True):
        with pytest.raises(ValueError, match="one-based"):
            RankedItem("a", rank)
    with pytest.raises(ValueError, match="candidate_id"):
        RankedItem(" ", 1)
    for score in (float("nan"), float("inf"), float("-inf"), True, "0.5"):
        with pytest.raises(ValueError, match="finite"):
            RankedItem("a", 1, score)  # type: ignore[arg-type]
    for fields in (("", "logit", None), ("model", "", None), ("model", "logit", " ")):
        with pytest.raises(ValueError):
            RerankResult((), fields[0], fields[1], fields[2])


def test_local_reranks_every_candidate_in_batches_with_stable_ties(
    fake_torch: None,
) -> None:
    async def run() -> None:
        model, tokenizer = Model([0.2, 0.8, 0.8]), Tokenizer()
        adapter: AsyncReranker = reranker(model, tokenizer, batch_size=2)
        candidates = [
            ContentItem("first", "same"),
            ContentItem("second", "same"),
            ContentItem("third", "same"),
        ]
        result = await adapter.rerank([TextPart(" Q"), TextPart(" ")], candidates)
        assert result == RerankResult(
            (
                RankedItem("second", 1, 0.8),
                RankedItem("third", 2, 0.8),
                RankedItem("first", 3, 0.2),
            ),
            "test-cross-encoder",
            "logit",
            revision="fixed-revision",
        )
        result.validate(candidates)
        assert model.calls == 2 and [len(batch) for batch in tokenizer.pad_batches] == [
            2,
            1,
        ]
        assert tokenizer.pairs == [(" Q ", "same")] * 3
        assert tokenizer.calls == [
            {"add_special_tokens": True, "padding": False, "truncation": False}
        ]
        assert model.evaluated and model.device == "cpu" and not model.closed
        assert candidates == [
            ContentItem("first", "same"),
            ContentItem("second", "same"),
            ContentItem("third", "same"),
        ]

    asyncio.run(run())


def test_empty_candidates_do_not_tokenize_or_infer(fake_torch: None) -> None:
    async def run() -> None:
        model, tokenizer = Model([]), Tokenizer()
        result = await reranker(model, tokenizer).rerank("query", [])
        assert result == RerankResult(
            (), "test-cross-encoder", "logit", "fixed-revision"
        )
        assert model.calls == 0 and tokenizer.calls == []

    asyncio.run(run())


def test_duplicate_ids_and_any_image_fail_before_model_access(fake_torch: None) -> None:
    async def run() -> None:
        model, tokenizer = Model([0.0]), Tokenizer()
        adapter = reranker(model, tokenizer)
        image = ImagePart(MediaRef(path="/not-read.png"))
        cases = [
            ("q", [ContentItem("same", "a"), ContentItem("same", "b")]),
            ([image], [ContentItem("a", "text")]),
            ("q", [ContentItem("a", [image])]),
            ([TextPart("q"), image], [ContentItem("a", "text")]),
            (
                "q",
                [
                    ContentItem("a", "text"),
                    ContentItem("b", [TextPart("caption"), image]),
                ],
            ),
        ]
        for query, candidates in cases:
            with pytest.raises(ValueError):
                await adapter.rerank(query, candidates)
        assert tokenizer.calls == [] and model.calls == 0

    asyncio.run(run())


def test_token_budget_rejects_complete_batch_without_truncation_or_partial_model_work(
    fake_torch: None,
) -> None:
    async def run() -> None:
        model, tokenizer = Model([1.0, 2.0]), Tokenizer()
        adapter = reranker(model, tokenizer, max_tokens=10, batch_size=1)
        with pytest.raises(ValueError, match="token budget"):
            await adapter.rerank(
                "q", [ContentItem("short", "a"), ContentItem("long", "x" * 10)]
            )
        assert model.calls == 0 and tokenizer.pad_batches == []
        assert tokenizer.calls[0]["truncation"] is False
        boundary = await adapter.rerank("q", [ContentItem("exact", "x" * 6)])
        assert boundary.items == (RankedItem("exact", 1, 1.0),)
        assert adapter.max_tokens == 10

    asyncio.run(run())


def test_local_rejects_missing_multiple_nonfinite_logits_and_failure_without_partial_result(
    fake_torch: None,
) -> None:
    class Malformed(Model):
        def __init__(self, shape: tuple[int, ...], score: float = 0.0) -> None:
            super().__init__([score])
            self.shape = shape

        def __call__(self, **inputs: Any) -> SimpleNamespace:
            return SimpleNamespace(logits=Tensor([[self.scores[0]]], shape=self.shape))

    class Failing(Model):
        def __call__(self, **inputs: Any) -> SimpleNamespace:
            if self.calls:
                raise RuntimeError("second model batch failed")
            return super().__call__(**inputs)

    async def run() -> None:
        for shape in ((0, 1), (1, 2), (1,), (2, 1)):
            with pytest.raises(ValueError, match="one logit"):
                await reranker(Malformed(shape)).rerank("q", [ContentItem("a", "a")])
        for score in (float("nan"), float("inf")):
            with pytest.raises(ValueError, match="finite"):
                await reranker(Malformed((1, 1), score)).rerank(
                    "q", [ContentItem("a", "a")]
                )
        model = Failing([1.0, 2.0])
        with pytest.raises(RuntimeError, match="second model batch"):
            await reranker(model, batch_size=1).rerank(
                "q", [ContentItem("a", "a"), ContentItem("b", "b")]
            )
        assert model.calls == 1 and not model.closed

    asyncio.run(run())


def test_local_validates_configuration_and_effective_model_budget() -> None:
    for value in (0, True):
        with pytest.raises(ValueError):
            reranker(batch_size=value)
        with pytest.raises(ValueError):
            reranker(max_tokens=value)
    for name in ("model_id", "revision", "device"):
        fields: dict[str, Any] = {
            "model": Model([0.0]),
            "tokenizer": Tokenizer(),
            "model_id": "model",
            "revision": "revision",
            name: " ",
        }
        with pytest.raises(ValueError, match="nonblank"):
            local.AsyncLocalReranker(**fields)
    model = Model([0.0])
    model.config.num_labels = 2
    with pytest.raises(ValueError, match="exactly one"):
        reranker(model)
    model.config.num_labels = 1
    model.config.max_position_embeddings = 12
    tokenizer = Tokenizer()
    tokenizer.model_max_length = 10
    assert reranker(model, tokenizer, max_tokens=20).max_tokens == 10
    model.config.max_position_embeddings = None
    tokenizer.model_max_length = 10**30
    with pytest.raises(ValueError, match="finite model token budget"):
        reranker(model, tokenizer)
    assert reranker(model, tokenizer, max_tokens=20).max_tokens == 20


def test_load_uses_only_existing_local_directory_safe_weights_and_no_remote_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Path, dict[str, Any]]] = []
    model, tokenizer = Model([0.0]), Tokenizer()

    class AutoModel:
        @staticmethod
        def from_pretrained(directory: Path, **kwargs: Any) -> Model:
            calls.append(("model", directory, kwargs))
            return model

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(directory: Path, **kwargs: Any) -> Tokenizer:
            calls.append(("tokenizer", directory, kwargs))
            return tokenizer

    def sdk(name: str) -> Any:
        assert name == "transformers"
        return SimpleNamespace(
            AutoModelForSequenceClassification=AutoModel, AutoTokenizer=AutoTokenizer
        )

    monkeypatch.setattr(local, "import_module", sdk)

    async def run() -> None:
        adapter = await local.AsyncLocalReranker.load(
            tmp_path, model_id="model", revision="immutable-revision"
        )
        assert (
            adapter.model is model
            and adapter.tokenizer is tokenizer
            and adapter.revision == "immutable-revision"
        )
        assert calls == [
            (
                "model",
                tmp_path.resolve(),
                {
                    "local_files_only": True,
                    "use_safetensors": True,
                    "trust_remote_code": False,
                },
            ),
            (
                "tokenizer",
                tmp_path.resolve(),
                {"local_files_only": True, "trust_remote_code": False},
            ),
        ]
        with pytest.raises(FileNotFoundError):
            await local.AsyncLocalReranker.load(
                tmp_path / "missing", model_id="model", revision="revision"
            )
        file = tmp_path / "regular-file"
        file.write_bytes(b"data")
        with pytest.raises(ValueError, match="local directory"):
            await local.AsyncLocalReranker.load(
                file, model_id="model", revision="revision"
            )
        assert len(calls) == 2

    asyncio.run(run())


def test_cancellation_waits_for_inference_and_lock_serializes_calls(
    fake_torch: None,
) -> None:
    entered, release = threading.Event(), threading.Event()
    active = 0
    high_water = 0
    counter_lock = threading.Lock()

    class Blocking(Model):
        def __call__(self, **inputs: Any) -> SimpleNamespace:
            nonlocal active, high_water
            with counter_lock:
                active += 1
                high_water = max(high_water, active)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test inference was not released")
            try:
                return super().__call__(**inputs)
            finally:
                with counter_lock:
                    active -= 1

    async def run() -> None:
        model = Blocking([1.0, 2.0])
        adapter = reranker(model)
        first = asyncio.create_task(adapter.rerank("q", [ContentItem("a", "a")]))
        second: asyncio.Task[RerankResult] | None = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            second = asyncio.create_task(adapter.rerank("q", [ContentItem("b", "b")]))
            first.cancel()
            await asyncio.sleep(0)
            assert not first.done() and active == 1
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert (await second).items == (RankedItem("b", 1, 2.0),)
            assert model.calls == 2 and high_water == 1 and not model.closed
        finally:
            release.set()
            await asyncio.gather(
                first, *([second] if second is not None else []), return_exceptions=True
            )

    asyncio.run(run())


def test_reranker_modules_do_not_import_optional_sdks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    original = builtins.__import__

    def guarded(name: str, *args: Any, **kwargs: Any) -> Any:
        assert name.split(".")[0] not in {"torch", "transformers"}
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    importlib.reload(importlib.import_module("comet_rag.rerankers"))
    importlib.reload(local)
