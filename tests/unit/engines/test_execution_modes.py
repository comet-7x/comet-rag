from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from comet_rag.engines.documents.docx import (
    AsyncDocxCleaner,
    AsyncDocxDocumentExtractor,
    DocxCleaner,
    DocxConverter,
    DocxDocumentExtractor,
    DocxParser,
)
from comet_rag.engines.documents.docx.types import DocxParsedContent
from comet_rag.engines.offload import run_sync
from comet_rag.engines.pipelines import AsyncHookRegistry, HookRegistry, PipelineConfig
from comet_rag.infrastructure.sources import AsyncBaseLoader
from comet_rag.ports import ExtractedDocument, LoadedResource, SourceContent
from comet_rag.services.pipeline import AsyncPipeline


def test_cpu_parsing_has_one_execution_interface() -> None:
    assert not hasattr(DocxParser, "aparse")
    assert not hasattr(DocxConverter, "ato_docx")
    assert not hasattr(DocxCleaner, "aclean_to_markdown")
    assert not hasattr(AsyncDocxCleaner, "aclean_to_blocks")
    assert not hasattr(DocxDocumentExtractor, "aextract")
    assert not hasattr(AsyncDocxDocumentExtractor, "aextract")


async def test_registries_keep_execution_modes_isolated(tmp_path: Path) -> None:
    sync, asynchronous = HookRegistry(), AsyncHookRegistry()
    content = LoadedResource(path=tmp_path / "sample", source=SourceContent("sample"))

    @sync.extractor("sample")
    def extract_sync(content, config) -> ExtractedDocument:
        return ExtractedDocument(markdown="sync")

    @asynchronous.extractor("sample")
    async def extract_async(content, config) -> ExtractedDocument:
        return ExtractedDocument(markdown="async")

    assert sync.extract("sample", content, PipelineConfig()).markdown == "sync"
    assert (
        await asynchronous.extract("sample", content, PipelineConfig())
    ).markdown == "async"
    fork = asynchronous.fork()
    with asynchronous.temporary():
        asynchronous.extractor("sample")(
            lambda content, config: asyncio.sleep(
                0, result=ExtractedDocument(markdown="replacement")
            )
        )
        assert (
            await fork.extract("sample", content, PipelineConfig())
        ).markdown == "async"
    assert asynchronous.get_extractor("sample") is extract_async


@pytest.mark.parametrize("late_failure", [False, True])
async def test_cancelled_sync_work_finishes_before_input_cleanup(
    tmp_path: Path, late_failure: bool
) -> None:
    path = tmp_path / "temporary.txt"
    path.write_text("input", encoding="utf-8")
    resource = LoadedResource(
        path, SourceContent(path), is_temp=True, metadata={"file_type": "blocking"}
    )
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    class Loader(AsyncBaseLoader):
        async def _load(self, source, **kwargs) -> LoadedResource:
            return resource

        async def cleanup(self) -> None:
            resource.cleanup()

    hooks = AsyncHookRegistry()

    @hooks.sync_extractor("blocking")
    def extract(content, config) -> ExtractedDocument:
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(2), "test did not release worker"
            assert content.path.read_text(encoding="utf-8") == "input"
            if late_failure:
                raise OSError("worker failed after cancellation")
            return ExtractedDocument(markdown="output")
        finally:
            finished.set()

    pipeline = AsyncPipeline(loader=Loader(), hooks=hooks)
    pending = asyncio.create_task(pipeline.run(path))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        assert path.exists()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, timeout=2)
    assert finished.is_set()
    assert not path.exists()


async def test_sync_bridge_preserves_worker_error_identity() -> None:
    error = OSError("read failed")

    def fail() -> None:
        raise error

    with pytest.raises(OSError) as caught:
        await run_sync(fail)
    assert caught.value is error


@pytest.mark.parametrize("fail", [False, True])
async def test_docx_cleaners_use_matching_vision_interface(fail: bool) -> None:
    calls: list[str] = []

    class Vision:
        def describe(self, base64_data: str, media_type: str) -> str:
            calls.append("sync")
            if fail:
                raise ConnectionError("vision unavailable")
            return "caption"

    class AsyncVision:
        async def describe(self, base64_data: str, media_type: str) -> str:
            calls.append("async")
            if fail:
                raise ConnectionError("vision unavailable")
            return "caption"

    document = DocxParsedContent(
        blocks=[
            {
                "type": "image",
                "content": "aGVsbG8=",
                "format": "png",
                "alt_text": "fallback",
            }
        ],
        metadata={},
    )
    sync = DocxCleaner(vision_model=Vision()).clean_to_markdown(document)
    asynchronous = await AsyncDocxCleaner(vision_model=AsyncVision()).clean_to_markdown(
        document
    )
    assert sync == asynchronous == ("[image: fallback]" if fail else "caption")
    assert calls == ["sync", "async"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("component", ["cleaner", "extractor"])
def test_docx_rejects_mismatched_vision_before_calling_it(
    asynchronous: bool, component: str
) -> None:
    calls: list[str] = []

    class Vision:
        def describe(self, base64_data: str, media_type: str) -> str:
            calls.append("sync")
            return "caption"

    class AsyncVision:
        async def describe(self, base64_data: str, media_type: str) -> str:
            calls.append("async")
            return "caption"

    wrong_model = Vision() if asynchronous else AsyncVision()
    cleaner = AsyncDocxCleaner if asynchronous else DocxCleaner
    extractor = AsyncDocxDocumentExtractor if asynchronous else DocxDocumentExtractor
    constructor = cleaner if component == "cleaner" else extractor
    with pytest.raises(TypeError, match="DOCX 此执行路径需要"):
        constructor(vision_model=wrong_model)  # type: ignore[arg-type] — 故意注入错误执行模型
    assert calls == []


async def test_docx_vision_cancellation_drains_other_descriptions() -> None:
    started = asyncio.Event()
    drained = asyncio.Event()

    class Vision:
        async def describe(self, base64_data: str, media_type: str) -> str:
            if base64_data == "cancel":
                await started.wait()
                raise asyncio.CancelledError
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
            return "unreachable"

    content = DocxParsedContent(
        blocks=[
            {"type": "image", "content": data, "format": "png"}
            for data in ("cancel", "blocked")
        ],
        metadata={},
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            AsyncDocxCleaner(vision_model=Vision()).clean_to_markdown(content),
            timeout=1,
        )
    assert drained.is_set()
