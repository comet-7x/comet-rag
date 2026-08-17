"""Pipeline 的 embedding 路径：窗口化并发与流式语义（spec S4-3）。

修复前 `astream_run` 是逐 chunk `await aembed()` —— **完全串行**，
200 个 chunk 就是 200 次依次排队的网络往返，模型服务大部分时间在空转。
而同一个类里 `_aembed_chunks` 走的是批量排程（并发）。
同一份工作两种写法，快慢差一个数量级。

注意收益来自**并发**而非请求数：本用例里的替身 `batch_limit == 1`，排程是
扇出 N 个单条请求并用信号量限流，不是把多条塞进一个请求。
真正的请求级批量需要模型层支持，属后续优化。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.pipelines import Pipeline, PipelineConfig, PipelineHooks
from comet_rag.infrastructure.models.embedding.base import BaseEmbeddingModel

CHUNK_COUNT = 200
STUB_TYPE = "stub"


# ── 测试替身 ───────────────────────────────────────────────────────────────


class RecordingEmbeddingModel(BaseEmbeddingModel):
    """记录调用次数与并发峰值的假模型。不打网络。"""

    def __init__(self, latency: float = 0.001) -> None:
        self.calls = 0
        self.live = 0
        self.peak = 0
        self._latency = latency
        self._lock = asyncio.Lock()

    def embed(self, data, **kwargs) -> list[float]:
        self.calls += 1
        return [float(len(str(data)))]

    async def _aembed(self, data, **kwargs) -> list[float]:
        self.calls += 1
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            await asyncio.sleep(self._latency)
        finally:
            self.live -= 1
        return [float(len(str(data)))]

    async def close_client(self) -> None:  # pragma: no cover - 无资源可释放
        return None


class StubLoader(BaseLoader):
    """把任意 source 映射到一个固定的临时文件，避开真实文件类型探测。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, source: SourceContent | str, *args, **kwargs) -> LoaderContent:
        if not isinstance(source, SourceContent):
            source = SourceContent(str(source))
        return LoaderContent(
            path=self._path,
            source=source,
            is_temp=False,
            metadata={"file_type": STUB_TYPE},
        )

    async def aload(
        self, source: SourceContent | str, *args, **kwargs
    ) -> LoaderContent:
        return self.load(source)

    def cleanup(self) -> None:
        return None


# ── 夹具 ───────────────────────────────────────────────────────────────────


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    path = tmp_path / "sample.stub"
    path.write_text("占位内容", encoding="utf-8")
    return path


@pytest.fixture
def model() -> RecordingEmbeddingModel:
    return RecordingEmbeddingModel()


@pytest.fixture
def make_pipeline(source_file: Path, model: RecordingEmbeddingModel):
    """构造一个必定切出 CHUNK_COUNT 个 chunk 的 pipeline。

    hook 注册由 conftest 的 autouse 夹具在用例结束后自动还原（T11）。
    """

    def _factory(*, embed: bool = True, embed_batch_size: int = 32, **kw) -> Pipeline:
        @PipelineHooks.extractor(STUB_TYPE)
        def _extract(lc: LoaderContent, config: PipelineConfig) -> str:
            return "x"

        @PipelineHooks.chunker(STUB_TYPE)
        def _chunk(text: str, config: PipelineConfig) -> list[str]:
            return [f"chunk-{i}" for i in range(CHUNK_COUNT)]

        config = PipelineConfig(embed=embed, embed_batch_size=embed_batch_size, **kw)
        return Pipeline(
            config=config, loader=StubLoader(source_file), embedding_model=model
        )

    return _factory


# ── 并发（S4-3 的实质）─────────────────────────────────────────────────────


async def test_astream_run_embeds_concurrently(
    make_pipeline, model: RecordingEmbeddingModel
) -> None:
    """修复前这里的 peak 恒为 1 —— 完全串行。"""
    pipeline = make_pipeline(max_concurrency=8, embed_batch_size=32)

    chunks = [c async for c in pipeline.astream_run("任意")]

    assert len(chunks) == CHUNK_COUNT
    assert model.peak > 1, "并发峰值为 1 说明仍在逐条串行"
    assert model.peak <= 8, f"并发峰值 {model.peak} 超过 max_concurrency=8"


async def test_arun_respects_concurrency_cap(
    make_pipeline, model: RecordingEmbeddingModel
) -> None:
    pipeline = make_pipeline(max_concurrency=4)

    await pipeline.arun("任意")

    assert 1 < model.peak <= 4


async def test_window_size_caps_in_flight_work(
    make_pipeline, model: RecordingEmbeddingModel
) -> None:
    """窗口比并发上限更小时，窗口才是实际约束。"""
    pipeline = make_pipeline(max_concurrency=64, embed_batch_size=3)

    await pipeline.arun("任意")

    assert model.peak <= 3


# ── 流式语义 ───────────────────────────────────────────────────────────────


async def test_first_chunk_arrives_before_all_are_embedded(
    make_pipeline, model: RecordingEmbeddingModel
) -> None:
    """窗口化不得退化成"全算完再一次性吐"—— 那样流式就没意义了。"""
    pipeline = make_pipeline(embed_batch_size=16)

    agen = pipeline.astream_run("任意")
    first = await anext(agen)

    assert first.embedding is not None
    assert model.calls <= 16, (
        f"产出首个 chunk 时已 embed {model.calls} 个，说明没有按窗口流式产出"
    )
    await agen.aclose()


def test_sync_stream_is_also_windowed(
    make_pipeline, model: RecordingEmbeddingModel
) -> None:
    pipeline = make_pipeline(embed_batch_size=16)

    gen = pipeline.stream_run("任意")
    first = next(gen)

    assert first.embedding is not None
    assert model.calls <= 16
    gen.close()


# ── 四个入口一致 ───────────────────────────────────────────────────────────


async def test_all_entry_points_embed_every_chunk(make_pipeline) -> None:
    """run / arun / stream_run / astream_run 的 embedding 行为必须一致。"""
    assert all(c.embedding for c in make_pipeline().run("任意").chunks)
    assert all(c.embedding for c in (await make_pipeline().arun("任意")).chunks)
    assert all(c.embedding for c in make_pipeline().stream_run("任意"))
    assert all([c.embedding async for c in make_pipeline().astream_run("任意")])


async def test_embed_disabled_leaves_embedding_none(make_pipeline) -> None:
    pipeline = make_pipeline(embed=False)

    result = await pipeline.arun("任意")

    assert len(result.chunks) == CHUNK_COUNT
    assert all(c.embedding is None for c in result.chunks)


async def test_every_chunk_is_embedded_exactly_once(
    make_pipeline, model: RecordingEmbeddingModel
) -> None:
    """窗口切分不得漏掉或重复处理边界上的 chunk。"""
    pipeline = make_pipeline(embed_batch_size=7)  # 200 不能被 7 整除

    chunks = [c async for c in pipeline.astream_run("任意")]

    assert len(chunks) == CHUNK_COUNT
    assert model.calls == CHUNK_COUNT
    assert [c.metadata["chunk_index"] for c in chunks] == list(range(CHUNK_COUNT))
