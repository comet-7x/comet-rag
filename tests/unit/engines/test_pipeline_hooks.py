"""`PipelineHooks` 注册表与作用域隔离。

注册表是进程级全局的（这让扩展格式只需 import 一个模块即可生效），
代价是注册会互相泄漏。本文件既验证注册分发本身，也验证隔离机制 ——
后者是 T12/T13 那些要临时注册 hook 的测试能安全并存的前提。
"""

from __future__ import annotations

import pytest

from comet_rag.engines.loaders.types import LoaderContent
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks


def _stub_extractor(lc: LoaderContent, config: PipelineConfig) -> str:
    return "stub"


def _stub_chunker(text: str, config: PipelineConfig) -> list[str]:
    return [text]


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


def test_unknown_extractor_raises_with_helpful_message() -> None:
    """报错要带上已注册列表，否则用户只能去翻源码找自己漏了什么。"""
    with pytest.raises(ValueError, match="No extractor registered"):
        PipelineHooks.get_extractor("从未注册过")


def test_chunker_falls_back_when_unregistered() -> None:
    """chunker 是可选的：没注册就回退 TextChunker，而不是报错。"""
    fallback = PipelineHooks.get_chunker("从未注册过")

    assert callable(fallback)
    assert fallback("一段文本。" * 50, PipelineConfig(chunk_size=50, chunk_overlap=5))


def test_chunker_dispatches_when_registered() -> None:
    PipelineHooks.chunker("zzz")(_stub_chunker)

    assert PipelineHooks.get_chunker("zzz") is _stub_chunker


def test_builtin_docx_hooks_are_registered() -> None:
    assert PipelineHooks.get_extractor("docx") is not None
    assert PipelineHooks.get_extractor("doc") is not None


# ── 隔离（P8）───────────────────────────────────────────────────────────────
#
# 下面两个用例**注册同名 extractor 但期望不同结果**。没有 conftest 里那个
# autouse 隔离夹具，它们的成败会取决于执行顺序 —— 这正是 P8 的原始症状。


def test_same_name_registration_case_a() -> None:
    def only_a(lc: LoaderContent, config: PipelineConfig) -> str:
        return "A"

    PipelineHooks.extractor("txt")(only_a)
    assert PipelineHooks.get_extractor("txt") is only_a


def test_same_name_registration_case_b() -> None:
    def only_b(lc: LoaderContent, config: PipelineConfig) -> str:
        return "B"

    PipelineHooks.extractor("txt")(only_b)
    assert PipelineHooks.get_extractor("txt") is only_b


def test_registrations_do_not_leak_between_tests() -> None:
    """上面两个用例都注册过 txt，到这里必须已经不存在。"""
    with pytest.raises(ValueError):
        PipelineHooks.get_extractor("txt")


# ── snapshot / restore / temporary ─────────────────────────────────────────


def test_temporary_restores_on_exit() -> None:
    original = PipelineHooks.get_extractor("docx")

    with PipelineHooks.temporary():

        def override(lc: LoaderContent, config: PipelineConfig) -> str:
            return "覆盖版"

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
