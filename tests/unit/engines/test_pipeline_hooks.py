"""`PipelineHooks` 注册表与作用域隔离。

注册表是进程级全局的（这让扩展格式只需 import 一个模块即可生效），
代价是注册会互相泄漏。本文件既验证注册分发本身，也验证隔离机制 ——
后者是 T12/T13 那些要临时注册 hook 的测试能安全并存的前提。
"""

from __future__ import annotations

import threading
import warnings
from pathlib import Path

import pytest

from comet_rag.engines.pipelines import HooksState, PipelineConfig, PipelineHooks
from comet_rag.ports import ExtractedDocument, NormalizedDocument
from comet_rag.ports.source import LoadedResource, SourceContent

LoaderContent = LoadedResource


def _stub_extractor(
    lc: LoaderContent, config: PipelineConfig
) -> ExtractedDocument:
    return ExtractedDocument(markdown="stub")


def _stub_chunker(text: str, config: PipelineConfig) -> list[str]:
    return [text]


@pytest.fixture
def loader_content(tmp_path: Path) -> LoaderContent:
    path = tmp_path / "sample.zzz"
    path.write_text("占位", encoding="utf-8")
    return LoaderContent(path=path, source=SourceContent(path))


# ── 注册与分发 ─────────────────────────────────────────────────────────────


def test_extractor_dispatches_by_file_type() -> None:
    PipelineHooks.extractor("zzz")(_stub_extractor)

    assert PipelineHooks.get_extractor("zzz") is _stub_extractor


def test_registration_is_case_insensitive() -> None:
    """用户传 "PDF"、loader 给出 "pdf"，两边必须对得上。"""
    PipelineHooks.extractor("ZZZ")(_stub_extractor)

    assert PipelineHooks.get_extractor("zzz") is _stub_extractor


def test_one_hook_can_serve_multiple_types() -> None:
    PipelineHooks.extractor("aaa", "bbb")(_stub_extractor)

    assert PipelineHooks.get_extractor("aaa") is _stub_extractor
    assert PipelineHooks.get_extractor("bbb") is _stub_extractor


async def test_async_extractor_dispatches_by_file_type(
    loader_content: LoaderContent,
) -> None:
    async def extract(
        lc: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument:
        return ExtractedDocument(markdown="async")

    PipelineHooks.aextractor("ZZZ")(extract)

    assert PipelineHooks.get_aextractor("zzz") is extract
    assert (
        (
            await PipelineHooks.aextract("zzz", loader_content, PipelineConfig())
        ).markdown
        == "async"
    )


async def test_async_extractor_is_preferred_over_sync(
    loader_content: LoaderContent,
) -> None:
    def forbidden(
        lc: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument:
        raise AssertionError("异步入口不应调用同步 Hook")

    async def extract(
        lc: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument:
        return ExtractedDocument(markdown="async")

    PipelineHooks.extractor("zzz")(forbidden)
    PipelineHooks.aextractor("zzz")(extract)

    assert (
        (
            await PipelineHooks.aextract("zzz", loader_content, PipelineConfig())
        ).markdown
        == "async"
    )


async def test_async_extraction_falls_back_to_one_thread_call(
    loader_content: LoaderContent,
) -> None:
    caller_thread = threading.get_ident()
    hook_threads: list[int] = []

    def extract(lc: LoaderContent, config: PipelineConfig) -> ExtractedDocument:
        hook_threads.append(threading.get_ident())
        return ExtractedDocument(markdown="sync")

    PipelineHooks.extractor("zzz")(extract)

    result = await PipelineHooks.aextract("zzz", loader_content, PipelineConfig())

    assert result.markdown == "sync"
    assert len(hook_threads) == 1
    assert hook_threads[0] != caller_thread


def test_unknown_extractor_raises_with_helpful_message() -> None:
    """报错要带上已注册列表，否则用户只能去翻源码找自己漏了什么。"""
    with pytest.raises(ValueError, match="No extractor registered"):
        PipelineHooks.get_extractor("从未注册过")


def test_unknown_async_extractor_returns_none() -> None:
    assert PipelineHooks.get_aextractor("从未注册过") is None


def test_chunker_falls_back_when_unregistered() -> None:
    """chunker 是可选的：没注册就回退 RecursiveChunker，而不是报错。"""
    fallback = PipelineHooks.get_chunker("从未注册过")

    assert callable(fallback)
    assert fallback("一段文本。" * 50, PipelineConfig(chunk_size=50, chunk_overlap=5))


def test_chunker_dispatches_when_registered() -> None:
    PipelineHooks.chunker("zzz")(_stub_chunker)

    assert PipelineHooks.get_chunker("zzz") is _stub_chunker


def test_builtin_docx_hooks_are_registered() -> None:
    assert PipelineHooks.get_extractor("docx") is not None
    assert PipelineHooks.get_extractor("doc") is not None


@pytest.mark.parametrize("file_type", ["docx", "unregistered"])
def test_builtin_and_default_document_chunkers_do_not_use_legacy_adapter(
    file_type: str,
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        drafts = PipelineHooks.get_document_chunker(file_type)(
            NormalizedDocument(markdown="正文。" * 20),
            PipelineConfig(chunk_size=30, chunk_overlap=3),
        )

    assert drafts
    assert not any(issubclass(item.category, DeprecationWarning) for item in caught)


# ── 隔离（P8）───────────────────────────────────────────────────────────────
#
# 下面两个用例**注册同名 extractor 但期望不同结果**。没有 conftest 里那个
# autouse 隔离夹具，它们的成败会取决于执行顺序 —— 这正是 P8 的原始症状。


def test_same_name_registration_case_a() -> None:
    def only_a(lc: LoaderContent, config: PipelineConfig) -> ExtractedDocument:
        return ExtractedDocument(markdown="A")

    PipelineHooks.extractor("txt")(only_a)
    assert PipelineHooks.get_extractor("txt") is only_a


def test_same_name_registration_case_b() -> None:
    def only_b(lc: LoaderContent, config: PipelineConfig) -> ExtractedDocument:
        return ExtractedDocument(markdown="B")

    PipelineHooks.extractor("txt")(only_b)
    assert PipelineHooks.get_extractor("txt") is only_b


def test_registrations_do_not_leak_between_tests() -> None:
    """上面两个用例都注册过 txt，到这里必须已经不存在。"""
    with pytest.raises(ValueError):
        PipelineHooks.get_extractor("txt")
    assert PipelineHooks.get_aextractor("txt") is None


# ── snapshot / restore / temporary ─────────────────────────────────────────


def test_temporary_restores_on_exit() -> None:
    original = PipelineHooks.get_extractor("docx")

    with PipelineHooks.temporary():

        def override(
            lc: LoaderContent, config: PipelineConfig
        ) -> ExtractedDocument:
            return ExtractedDocument(markdown="覆盖版")

        PipelineHooks.extractor("docx")(override)
        assert PipelineHooks.get_extractor("docx") is override

    assert PipelineHooks.get_extractor("docx") is original


def test_temporary_restores_on_exception() -> None:
    """异常路径也必须还原，否则一次失败会污染整个进程。"""
    original = PipelineHooks.get_extractor("docx")

    with pytest.raises(RuntimeError), PipelineHooks.temporary():
        PipelineHooks.extractor("docx")(_stub_extractor)
        raise RuntimeError("炸了")

    assert PipelineHooks.get_extractor("docx") is original


def test_temporary_discards_new_registrations() -> None:
    with PipelineHooks.temporary():
        PipelineHooks.extractor("qqq")(_stub_extractor)

    with pytest.raises(ValueError):
        PipelineHooks.get_extractor("qqq")


def test_snapshot_is_not_a_live_view() -> None:
    """快照必须是拷贝：拿到引用后再注册，还原就会把新注册也带回来。"""
    state = PipelineHooks.snapshot()
    PipelineHooks.extractor("www")(_stub_extractor)

    PipelineHooks.restore(state)

    with pytest.raises(ValueError):
        PipelineHooks.get_extractor("www")


def test_snapshot_restores_async_extractors() -> None:
    state = PipelineHooks.snapshot()

    async def extract(
        lc: LoaderContent, config: PipelineConfig
    ) -> ExtractedDocument:
        return ExtractedDocument(markdown="async")

    PipelineHooks.aextractor("www")(extract)
    PipelineHooks.restore(state)

    assert PipelineHooks.get_aextractor("www") is None


def test_hooks_state_keeps_original_positional_constructor() -> None:
    state = HooksState({"zzz": _stub_extractor}, {"zzz": _stub_chunker})

    assert state.async_extractors == {}
