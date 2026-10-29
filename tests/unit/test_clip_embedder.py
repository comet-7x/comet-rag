from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self, cast

import pytest

from comet_rag.content import ContentItem, ImagePart, MediaRef, TextPart
from comet_rag.embedder.models import clip
from comet_rag.embedder.types import (
    AsyncContentEmbedder,
    EmbeddingResult,
    EmbeddingSpace,
)


class Row:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def cpu(self) -> Row:
        return self

    def tolist(self) -> list[float]:
        return self.values


class Tensor:
    def __init__(self, values: list[list[float]]) -> None:
        self.values = values
        self.shape = (len(values), len(values[0]) if values else 0)

    def norm(self, *, dim: int, keepdim: bool) -> tuple[float, ...]:
        assert dim == -1 and keepdim is True
        return tuple(
            math.sqrt(sum(value * value for value in row)) for row in self.values
        )

    def __truediv__(self, norms: tuple[float, ...]) -> Tensor:
        return Tensor(
            [
                [value / norm if norm else float("nan") for value in row]
                for row, norm in zip(self.values, norms, strict=True)
            ]
        )

    def __getitem__(self, index: int) -> Row:
        return Row(self.values[index])


class Batch(dict[str, Any]):
    def to(self, device: str) -> Batch:
        self["device"] = device
        return self


class Processor:
    def __init__(self, *, tokens: int = 4) -> None:
        self.tokens = tokens
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Batch:
        self.calls.append(kwargs)
        if "images" in kwargs:
            return Batch(pixel_values="image-tensor")
        return Batch(input_ids=SimpleNamespace(shape=(1, self.tokens)))


class Model:
    def __init__(self, vector: tuple[float, ...] = (3.0, 4.0)) -> None:
        self.vector = vector
        self.config = SimpleNamespace(
            projection_dim=2, text_config=SimpleNamespace(max_position_embeddings=4)
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.device: str | None = None
        self.evaluated = False

    def eval(self) -> Model:
        self.evaluated = True
        return self

    def to(self, device: str) -> Model:
        self.device = device
        return self

    def get_text_features(self, **inputs: Any) -> Tensor:
        assert inputs["device"] == self.device
        self.calls.append(("text", inputs))
        return Tensor([list(self.vector)])

    def get_image_features(self, **inputs: Any) -> Tensor:
        assert inputs["device"] == self.device
        self.calls.append(("image", inputs))
        return Tensor([list(self.vector)])


class Runtime:
    def __init__(self) -> None:
        self.opened: list[bytes] = []
        self.events: list[str] = []
        self.imports: list[str] = []
        self.inference_threads: list[int] = []
        self.size = (3, 2)

    @contextmanager
    def inference_mode(self) -> Iterator[None]:
        self.inference_threads.append(threading.get_ident())
        self.events.append("inference-enter")
        try:
            yield
        finally:
            self.events.append("inference-exit")

    def open(self, stream: BytesIO) -> Any:
        self.opened.append(stream.getvalue())
        runtime = self

        class Image:
            width, height = runtime.size

            def __enter__(self) -> Self:
                runtime.events.append("image-open")
                return self

            def __exit__(self, *args: object) -> None:
                runtime.events.append("image-close")

            def load(self) -> None:
                runtime.events.append("image-load")

            def convert(self, mode: str) -> SimpleNamespace:
                assert mode == "RGB"
                runtime.events.append("image-rgb")
                return SimpleNamespace(mode=mode, size=runtime.size)

        return Image()

    def sdk(self, name: str) -> Any:
        self.imports.append(name)
        if name == "torch":
            return SimpleNamespace(inference_mode=self.inference_mode)
        assert name == "PIL.Image"
        return SimpleNamespace(open=self.open)


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Runtime:
    runtime = Runtime()
    monkeypatch.setattr(clip, "import_module", runtime.sdk)
    return runtime


def _embedder(
    model: Model | None = None, processor: Processor | None = None, **kwargs: Any
) -> clip.AsyncCLIPEmbedder:
    return clip.AsyncCLIPEmbedder(
        model=model if model is not None else Model(),
        processor=processor if processor is not None else Processor(),
        space=EmbeddingSpace("fake-clip", 2, "injected", normalized=True),
        **kwargs,
    )


@pytest.mark.parametrize("role", ["query", "document"])
def test_clip_preserves_order_identity_text_whitespace_and_image_channel(
    runtime: Runtime, role: str
) -> None:
    async def scenario() -> None:
        model, processor = Model(), Processor()
        adapter: AsyncContentEmbedder = _embedder(model, processor)
        items = [
            ContentItem(
                "image",
                [ImagePart(MediaRef(data=b"image-bytes", media_type="image/png"))],
            ),
            ContentItem("text", [TextPart("  comet"), TextPart("\n star  ")]),
        ]
        result = await adapter.embed(items, role=cast("Any", role))
        assert tuple(item.item_id for item in result) == ("image", "text")
        assert all(item.vector == pytest.approx((0.6, 0.8)) for item in result)
        assert all(item.space_id == adapter.space.space_id for item in result)
        assert [kind for kind, _ in model.calls] == ["image", "text"]
        assert processor.calls[0]["images"].mode == "RGB"
        assert processor.calls[0]["images"].size == runtime.size
        assert processor.calls[0]["return_tensors"] == "pt"
        assert processor.calls[1] == {
            "text": ["  comet\n star  "],
            "padding": True,
            "truncation": False,
            "return_tensors": "pt",
        }
        assert runtime.opened == [b"image-bytes"]
        assert runtime.events == [
            "inference-enter",
            "image-open",
            "image-load",
            "image-rgb",
            "image-close",
            "inference-exit",
            "inference-enter",
            "inference-exit",
        ]
        assert model.evaluated and model.device == "cpu"
        assert threading.get_ident() not in runtime.inference_threads

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "parts",
    [
        [TextPart("caption"), ImagePart(MediaRef(path="/not-read.png"))],
        [ImagePart(MediaRef(path="/not-read.png")), TextPart("caption")],
        [
            ImagePart(MediaRef(path="/not-read.png")),
            ImagePart(MediaRef(path="/not-read-either.png")),
        ],
    ],
)
def test_clip_rejects_joint_or_multiple_images_without_silently_using_text(
    runtime: Runtime, parts: Any
) -> None:
    async def scenario() -> None:
        model, processor = Model(), Processor()
        with pytest.raises(ValueError, match="one image or text"):
            await _embedder(model, processor).embed(
                [ContentItem("item", parts)], role="document"
            )
        assert not model.calls and not processor.calls and not runtime.opened

    asyncio.run(scenario())


def test_clip_rejects_duplicate_ids_and_invalid_role_before_sdk_context(
    runtime: Runtime,
) -> None:
    async def scenario() -> None:
        model, processor = Model(), Processor()
        adapter = _embedder(model, processor)
        with pytest.raises(ValueError, match="duplicate IDs"):
            await adapter.embed(
                [ContentItem("same", "a"), ContentItem("same", "b")], role="query"
            )
        with pytest.raises(ValueError, match="role"):
            await adapter.embed([ContentItem("a", "text")], role=cast("Any", "invalid"))
        assert not runtime.imports and not model.calls and not processor.calls

    asyncio.run(scenario())


def test_clip_token_budget_refuses_truncation_and_accepts_exact_boundary(
    runtime: Runtime,
) -> None:
    async def scenario() -> None:
        model, processor = Model(), Processor(tokens=5)
        adapter = _embedder(model, processor)
        with pytest.raises(ValueError, match="token budget"):
            await adapter.embed([ContentItem("long", "long")], role="query")
        assert not model.calls
        assert processor.calls[0]["truncation"] is False
        processor.tokens = 4
        assert (await adapter.embed([ContentItem("exact", "exact")], role="query"))[
            0
        ].vector == pytest.approx((0.6, 0.8))
        assert len(model.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("local", [False, True])
def test_clip_media_budget_is_bounded_before_image_decoder(
    runtime: Runtime, tmp_path: Path, local: bool
) -> None:
    async def scenario() -> None:
        data = b"12345"
        path = tmp_path / "input.png"
        if local:
            path.write_bytes(data)
            reference = MediaRef(path=path)
        else:
            reference = MediaRef(data=data, media_type="image/png")
        model, processor = Model(), Processor()
        with pytest.raises(ValueError, match="byte budget"):
            await _embedder(model, processor, max_media_bytes=4).embed(
                [ContentItem("large", [ImagePart(reference)])], role="document"
            )
        assert not runtime.opened and not processor.calls and not model.calls
        result = await _embedder(model, processor, max_media_bytes=5).embed(
            [ContentItem("exact", [ImagePart(reference)])], role="document"
        )
        assert result[0].item_id == "exact" and runtime.opened == [data]
        if local:
            assert path.read_bytes() == data

    asyncio.run(scenario())


def test_clip_pixel_budget_precedes_load_conversion_and_inference(
    runtime: Runtime,
) -> None:
    async def scenario() -> None:
        model, processor = Model(), Processor()
        item = ContentItem(
            "pixels", [ImagePart(MediaRef(data=b"bytes", media_type="image/png"))]
        )
        with pytest.raises(ValueError, match="pixel budget"):
            await _embedder(model, processor, max_pixels=5).embed(
                [item], role="document"
            )
        assert "image-load" not in runtime.events and "image-rgb" not in runtime.events
        assert not processor.calls and not model.calls
        assert (
            await _embedder(model, processor, max_pixels=6).embed(
                [item], role="document"
            )
        )[0].item_id == "pixels"

    asyncio.run(scenario())


def test_clip_remote_images_require_materialization_without_network_access(
    runtime: Runtime,
) -> None:
    async def scenario() -> None:
        model, processor = Model(), Processor()
        item = ContentItem(
            "remote", [ImagePart(MediaRef(url="https://example.test/image.png"))]
        )
        with pytest.raises(ValueError, match="local or inline"):
            await _embedder(model, processor).embed([item], role="document")
        assert not runtime.opened and not processor.calls and not model.calls

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "vector",
    [(float("nan"), 1.0), (float("inf"), 1.0), (0.0, 0.0), (1.0,), (1.0, 2.0, 3.0)],
)
def test_clip_rejects_nonfinite_zero_norm_and_wrong_dimension_model_output(
    runtime: Runtime, vector: tuple[float, ...]
) -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError, match="dimension.*finite"):
            await _embedder(Model(vector)).embed(
                [ContentItem("bad-output", "text")], role="document"
            )

    asyncio.run(scenario())


def test_clip_empty_batch_does_not_call_processor_or_model(runtime: Runtime) -> None:
    async def scenario() -> None:
        model, processor = Model(), Processor()
        assert await _embedder(model, processor).embed([], role="query") == ()
        assert not model.calls and not processor.calls and not runtime.opened

    asyncio.run(scenario())


def test_clip_load_is_local_uses_safe_weights_and_records_preprocessing_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, processor = Model(), Processor()
    calls: list[tuple[str, Path, dict[str, Any]]] = []
    threads: list[int] = []
    (tmp_path / "config.json").write_bytes(b"config")
    (tmp_path / "model.safetensors").write_bytes(b"weights")
    (tmp_path / "preprocessor_config.json").write_bytes(b"processor")
    (tmp_path / "ignored.txt").write_bytes(b"not model state")
    (tmp_path / "tokenizer.json").mkdir()

    class CLIPModel:
        @staticmethod
        def from_pretrained(path: Path, **kwargs: Any) -> Model:
            threads.append(threading.get_ident())
            calls.append(("model", path, kwargs))
            return model

    class CLIPProcessor:
        @staticmethod
        def from_pretrained(path: Path, **kwargs: Any) -> Processor:
            calls.append(("processor", path, kwargs))
            return processor

    def sdk(name: str) -> Any:
        assert name == "transformers"
        return SimpleNamespace(CLIPModel=CLIPModel, CLIPProcessor=CLIPProcessor)

    monkeypatch.setattr(clip, "import_module", sdk)

    async def scenario() -> None:
        adapter = await clip.AsyncCLIPEmbedder.load(
            tmp_path, model_id="fixture-clip", revision="fixed-revision", device="cpu"
        )
        assert calls == [
            ("model", tmp_path, {"local_files_only": True, "use_safetensors": True}),
            ("processor", tmp_path, {"local_files_only": True, "use_fast": False}),
        ]
        assert adapter.model is model and adapter.processor is processor
        assert (
            adapter.space.model,
            adapter.space.version,
            adapter.space.dimension,
            adapter.space.deployment,
        ) == ("fixture-clip", "fixed-revision", 2, "local-transformers")
        assert adapter.space.normalized is True and adapter.space.metric == "COSINE"
        assert adapter.space.preprocessing == {
            "adapter": "clip_v1",
            "use_fast": False,
            "files": {
                name: hashlib.sha256(data).hexdigest()
                for name, data in (
                    ("config.json", b"config"),
                    ("model.safetensors", b"weights"),
                    ("preprocessor_config.json", b"processor"),
                )
            },
        }
        assert threading.get_ident() not in threads
        first_id = adapter.space.space_id
        (tmp_path / "config.json").write_bytes(b"changed config")
        updated = await clip.AsyncCLIPEmbedder.load(
            tmp_path, model_id="fixture-clip", revision="fixed-revision"
        )
        assert updated.space.space_id != first_id

    asyncio.run(scenario())


def test_clip_load_fingerprints_referenced_shards_and_complete_processor_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, processor = Model(), Processor()
    calls: list[str] = []

    def pretrained(kind: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(kind)
        return model if kind == "model" else processor

    sdk = SimpleNamespace(
        CLIPModel=SimpleNamespace(
            from_pretrained=lambda *args, **kwargs: pretrained("model", *args, **kwargs)
        ),
        CLIPProcessor=SimpleNamespace(
            from_pretrained=lambda *args, **kwargs: pretrained(
                "processor", *args, **kwargs
            )
        ),
    )
    monkeypatch.setattr(clip, "import_module", lambda name: sdk)
    contents = {
        "config.json": b"config",
        "preprocessor_config.json": b"image processor",
        "processor_config.json": b"processor state",
        "tokenizer_config.json": b"tokenizer",
        "added_tokens.json": b"additional vocabulary",
        "special_tokens_map.json": b"special tokens",
        "vocab.json": b"vocabulary",
        "merges.txt": b"merges",
        "model.safetensors.index.json": json.dumps(
            {
                "weight_map": {
                    "left": "weights/left.safetensors",
                    "right": "right.safetensors",
                }
            }
        ).encode(),
        "weights/left.safetensors": b"left weights",
        "right.safetensors": b"right weights",
    }
    for name, data in contents.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (tmp_path / "unused.safetensors").write_bytes(b"not loaded")
    (tmp_path / "README.md").write_bytes(b"not model input")

    async def scenario() -> None:
        first = await clip.AsyncCLIPEmbedder.load(
            tmp_path, model_id="fixture", revision="same"
        )
        assert first.space.preprocessing["files"] == {
            name: hashlib.sha256(data).hexdigest() for name, data in contents.items()
        }
        for name in (
            "weights/left.safetensors",
            "right.safetensors",
            "added_tokens.json",
            "processor_config.json",
            "model.safetensors.index.json",
        ):
            path = tmp_path / name
            original = path.read_bytes()
            path.write_bytes(
                original + (b" " if name.endswith(".json") else b"changed")
            )
            updated = await clip.AsyncCLIPEmbedder.load(
                tmp_path, model_id="fixture", revision="same"
            )
            assert updated.space.space_id != first.space.space_id
            path.write_bytes(original)
        assert calls == ["model", "processor"] * 6

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "shard",
    [
        "../outside.safetensors",
        "/absolute.safetensors",
        "weights\\\\outside.safetensors",
        "pytorch_model.bin",
        "",
        42,
    ],
)
def test_clip_rejects_invalid_or_missing_shards_before_loading_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shard: object
) -> None:
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"weight": shard}})
    )

    def sdk(name: str) -> Any:
        pytest.fail("invalid weight index must fail before loading models")

    monkeypatch.setattr(clip, "import_module", sdk)
    with pytest.raises(ValueError, match="shard index"):
        asyncio.run(
            clip.AsyncCLIPEmbedder.load(tmp_path, model_id="fixture", revision="same")
        )
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"weight": "missing.safetensors"}})
    )
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            clip.AsyncCLIPEmbedder.load(tmp_path, model_id="fixture", revision="same")
        )


def test_clip_cancellation_drains_inference_and_serializes_model_access(
    runtime: Runtime, tmp_path: Path
) -> None:
    entered, release = threading.Event(), threading.Event()
    active = peak = 0
    counter = threading.Lock()

    class Blocking(Model):
        def get_image_features(self, **inputs: Any) -> Tensor:
            nonlocal active, peak
            with counter:
                active += 1
                peak = max(peak, active)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("fake inference not released")
            try:
                return super().get_image_features(**inputs)
            finally:
                with counter:
                    active -= 1

    async def scenario() -> None:
        path = tmp_path / "input.png"
        path.write_bytes(b"borrowed-image")
        model = Blocking()
        adapter = _embedder(model)
        item = ContentItem("image", [ImagePart(MediaRef(path=path))])
        first = asyncio.create_task(adapter.embed([item], role="document"))
        second: asyncio.Task[tuple[EmbeddingResult, ...]] | None = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            second = asyncio.create_task(
                adapter.embed([ContentItem("second", item.content)], role="query")
            )
            for _ in range(2):
                first.cancel()
                await asyncio.sleep(0)
                assert not first.done() and active == 1
                assert "image-close" not in runtime.events
                assert path.read_bytes() == b"borrowed-image"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert (await second)[0].item_id == "second"
            assert peak == 1 and len(model.calls) == 2
            assert runtime.events.count("image-close") == 2
        finally:
            release.set()
            await asyncio.gather(
                first, *([second] if second is not None else []), return_exceptions=True
            )
        assert path.read_bytes() == b"borrowed-image"

    asyncio.run(scenario())


def test_clip_cancellation_stops_remaining_items(runtime: Runtime) -> None:
    entered, release = threading.Event(), threading.Event()

    class Blocking(Model):
        def get_text_features(self, **inputs: Any) -> Tensor:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("inference not released")
            return super().get_text_features(**inputs)

    async def scenario() -> None:
        model = Blocking()
        task = asyncio.create_task(
            _embedder(model).embed(
                [ContentItem("first", "a"), ContentItem("second", "b")],
                role="document",
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(model.calls) == 1
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_clip_applies_per_call_prefix_before_token_budget(runtime: Runtime) -> None:
    class TextProcessor(Processor):
        def __call__(self, **kwargs: Any) -> Batch:
            if "text" in kwargs:
                self.tokens = len(kwargs["text"][0])
            return super().__call__(**kwargs)

    async def scenario() -> None:
        model, processor = Model(), TextProcessor()
        adapter = clip.AsyncCLIPEmbedder(
            model=model,
            processor=processor,
            space=EmbeddingSpace(
                "fake-clip",
                2,
                "injected",
                normalized=True,
                query_prefix="q:",
                document_prefix="d:",
            ),
        )
        await asyncio.gather(
            adapter.embed([ContentItem("q", " x")], role="query"),
            adapter.embed([ContentItem("d", " y")], role="document"),
        )
        assert sorted(call["text"][0] for call in processor.calls) == ["d: y", "q: x"]
        with pytest.raises(ValueError, match="token budget"):
            await adapter.embed([ContentItem("over", "xyz")], role="query")
        assert len(model.calls) == 2
        image = ContentItem(
            "image", [ImagePart(MediaRef(data=b"i", media_type="image/png"))]
        )
        await adapter.embed([image], role="query")
        assert model.calls[-1][0] == "image"

    asyncio.run(scenario())


@pytest.mark.parametrize("rows", [[], [[3.0, 4.0], [4.0, 3.0]]])
def test_clip_requires_exactly_one_output_vector(
    runtime: Runtime, rows: list[list[float]]
) -> None:
    class WrongCount(Model):
        def get_text_features(self, **inputs: Any) -> Tensor:
            return Tensor(rows)

    async def scenario() -> None:
        with pytest.raises(ValueError, match="one vector"):
            await _embedder(WrongCount()).embed(
                [ContentItem("one", "text")], role="query"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_clip_budget_validation_precedes_model_initialization(budget: Any) -> None:
    model = Model()
    with pytest.raises(ValueError, match="positive integers"):
        _embedder(model, max_media_bytes=budget)
    with pytest.raises(ValueError, match="positive integers"):
        _embedder(model, max_pixels=budget)
    assert not model.evaluated and model.device is None
