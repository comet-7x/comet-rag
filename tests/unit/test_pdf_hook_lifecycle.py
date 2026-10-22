from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from comet_rag.composition.bootstrap import wire_pdf_extractor
from comet_rag.engines.pipelines import AsyncPipelineHooks
from comet_rag.ports import ExtractedDocument, LoadedResource, SourceContent
from comet_rag.services.pipeline import AsyncPipeline


class TemporaryPDFLoader:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def load(self, source: SourceContent | str) -> LoadedResource:
        return LoadedResource(
            path=self.path,
            source=SourceContent(self.path),
            is_temp=True,
            metadata={"file_type": "pdf"},
        )

    async def batch_load(
        self, sources: list[SourceContent] | list[str], *, max_concurrency: int
    ) -> list[LoadedResource]:
        raise AssertionError("this test must use the single-document path")

    async def cleanup(self) -> None:
        pass

    async def close(self) -> None:
        pass


class RecordingExtractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument:
        self.calls += 1
        return ExtractedDocument(markdown="upstream result")

    async def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("cancellations", "probe_fails"), [(1, False), (2, False), (2, True)]
)
async def test_cancelled_pdf_probe_finishes_before_pipeline_releases_its_file(
    cancellations: int,
    probe_fails: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF-1.7\nfixture")
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    input_available: list[bool] = []

    def blocking_probe(filename: str) -> str:
        entered.set()
        try:
            assert release.wait(2), "test did not release the content probe"
            input_available.append(Path(filename).is_file())
            if probe_fails:
                raise RuntimeError("content probe failed after cancellation")
            return "pdf"
        finally:
            finished.set()

    monkeypatch.setattr(
        "comet_rag.composition.bootstrap.detect_content_type_from_path",
        blocking_probe,
    )
    extractor = RecordingExtractor()
    hooks = AsyncPipelineHooks.fork()
    wire_pdf_extractor(extractor, hooks=hooks)
    pipeline = AsyncPipeline(loader=TemporaryPDFLoader(path), hooks=hooks)
    task = asyncio.create_task(pipeline.run(path))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
        _, pending = await asyncio.wait({task}, timeout=0.02)
        assert task in pending, "pipeline returned while its content probe was active"
        assert path.is_file(), "pipeline deleted the probe's input file too early"
        assert not finished.is_set()
        assert extractor.calls == 0
    finally:
        release.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]

    assert isinstance(outcome, asyncio.CancelledError)
    assert finished.is_set()
    assert input_available == [True]
    assert extractor.calls == 0
    assert not path.exists()
