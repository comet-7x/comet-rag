"""闸门覆盖守卫：**每一道公开的门都必须被归类。**

## 为什么需要它

同一类缺陷在一次评审里被抓到四次，每次都是"某个公开入口没过闸门"：

    1. embed_batch / embed_query / embed_document
    2. embed 本身
    3. BaseLoader.load
    4. BaseReranker.score

每次修完都以为齐了，下一轮又冒出一道。原因不是粗心，是**做法错了**：靠人回忆
"还有哪些入口"，而不是让结构来枚举。

漏掉一道门不会报错、不会打日志 —— 限流只是悄悄失效，而这个项目实测过一次
"配置写 4、实际 128"。所以这条守卫的判据不是"有没有加闸"，而是
**每个公开方法都必须被显式归入下面三类之一**：

    direct    —— 自己进闸门（体内出现 `_through_gate*`）
    indirect  —— 经由某个 direct 方法进闸门（会核对它确实调了）
    exempt    —— 不发下游请求（关闭、清理这类）

新增一个公开方法却没归类，测试当场失败。**白名单没有"忘了新增的那个"这种
失效模式**，黑名单有 —— 这与 `test_layering.py` 里 engines 白名单是同一条理由。
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any

import pytest

from comet_rag.core.concurrency import Gate
from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.infrastructure.providers.embedding.base import (
    BaseEmbeddingModel,
    MultimodalEmbeddingMixin,
)
from comet_rag.infrastructure.providers.reranker.base import BaseReranker
from comet_rag.ports.gate import GatedResource

#: 三类的含义见模块文档。改动这张表时请连同理由一起写。
CLASSIFICATION: dict[type, dict[str, set[str]]] = {
    BaseEmbeddingModel: {
        "direct": {"embed", "embed_batch", "embed_document", "embed_query", "aembed", "aembed_batch"},
        # 走 `aembed`，由它进闸门
        "indirect": {"aembed_document", "aembed_query"},
        "exempt": {"aclose"},
    },
    MultimodalEmbeddingMixin: {
        "direct": {"embed_media", "aembed_media"},
        "indirect": set(),
        "exempt": set(),
    },
    BaseReranker: {
        "direct": {"score", "ascore"},
        # 翻译成供应商格式之后走 `score`/`ascore`
        "indirect": {"rank", "arank"},
        "exempt": {"aclose"},
    },
    BaseLoader: {
        "direct": {"load", "aload"},
        # 每个来源各走一次 `load`/`aload`，所以是每次请求一个名额
        "indirect": {"batch_load", "abatch_load"},
        "exempt": {"cleanup", "acleanup"},
    },
}


def _self_attributes(method: Any) -> set[str]:
    """方法体里所有 `self.X` 的 X。

    用 AST 而不是找 `"self.X("` 字面量：`batch_load` 里是
    `executor.submit(self.load, source)` —— 把方法**当值传**，没有括号。
    守卫第一版就漏在这，报了一条假的。
    """
    source = textwrap.dedent(inspect.getsource(method))
    return {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }


def _public_methods(cls: type) -> dict[str, Any]:
    """类**自己定义**的公开方法。继承自 `GatedResource` 的不算。"""
    return {
        name: value
        for name, value in vars(cls).items()
        if not name.startswith("_") and callable(value)
    }


@pytest.mark.parametrize("cls", list(CLASSIFICATION), ids=lambda c: c.__name__)
def test_every_public_entry_point_is_classified(cls: type) -> None:
    """**新增一道门就必须表态它走不走闸门。**

    这条断言本身没有技术含量，它的价值在于"忘了"这件事会当场失败，而不是
    等到某轮评审、或者线上限流悄悄失效。
    """
    declared = set().union(*CLASSIFICATION[cls].values())
    actual = set(_public_methods(cls))

    unclassified = actual - declared
    assert not unclassified, (
        f"{cls.__name__} 新增了未归类的公开方法：{sorted(unclassified)}。"
        f"请在 CLASSIFICATION 里归入 direct / indirect / exempt，并写明理由 —— "
        f"漏掉一道门不会报错，只会让限流悄悄失效。"
    )
    stale = declared - actual
    assert not stale, f"{cls.__name__} 的分类表里有已不存在的方法：{sorted(stale)}"


@pytest.mark.parametrize("cls", list(CLASSIFICATION), ids=lambda c: c.__name__)
def test_direct_entries_really_enter_the_gate(cls: type) -> None:
    """归类为 direct 不能只是口头承诺 —— 体内必须真的进闸门。"""
    for name in sorted(CLASSIFICATION[cls]["direct"]):
        method = _public_methods(cls)[name]
        entry = "_through_gate" if inspect.iscoroutinefunction(method) else "_through_gate_sync"
        assert entry in _self_attributes(method), (
            f"{cls.__name__}.{name} 归类为 direct，但体内没有调 self.{entry}"
        )


@pytest.mark.parametrize("cls", list(CLASSIFICATION), ids=lambda c: c.__name__)
def test_indirect_entries_really_delegate_to_a_direct_one(cls: type) -> None:
    """indirect 同理：必须能指出它经由哪个 direct 方法进闸门。"""
    direct = CLASSIFICATION[cls]["direct"]
    for name in sorted(CLASSIFICATION[cls]["indirect"]):
        method = _public_methods(cls)[name]
        assert _self_attributes(method) & direct, (
            f"{cls.__name__}.{name} 归类为 indirect，但体内没有调用任何 direct 入口"
            f"（{sorted(direct)}）—— 它可能根本没经过闸门"
        )


@pytest.mark.parametrize("cls", list(CLASSIFICATION), ids=lambda c: c.__name__)
def test_direct_entries_cannot_be_overridden(cls: type) -> None:
    """**direct 入口必须是 `@final`。**

    否则子类一覆写就把闸门覆写掉了，而且不报错 —— 这正是模板方法拆成
    "final 外壳 + abstract 内核"的全部理由。
    """
    for name in sorted(CLASSIFICATION[cls]["direct"]):
        method = _public_methods(cls)[name]
        assert getattr(method, "__final__", False), (
            f"{cls.__name__}.{name} 是闸门入口却不是 @final，子类可以覆写掉闸门"
        )


def test_all_gated_classes_are_covered() -> None:
    """**新增一个受闸门保护的基类，也要进这张表。**

    否则整个类都在守卫视野之外 —— 那正是这条守卫要防的失效模式的更大版本。
    """
    known = set(CLASSIFICATION)
    missing = {
        cls
        for cls in _gated_subclasses(GatedResource)
        if cls not in known and inspect.isabstract(cls)
    }
    assert not missing, (
        f"这些受闸门保护的基类还没进分类表：{sorted(c.__name__ for c in missing)}"
    )


def _gated_subclasses(root: type) -> set[type]:
    found: set[type] = set()
    for child in root.__subclasses__():
        found.add(child)
        found |= _gated_subclasses(child)
    return found


# ── 第二层：行为守卫 ───────────────────────────────────────────────────────
#
# 上面那层是**静态**的：AST 里出现了 `self._through_gate_sync` 就算数。它证明不了
# 运行时真的进了闸门 —— 实现可以把调用放进走不到的分支，也可以换个别名或辅助
# 函数绕过字符串匹配（评审指出）。
#
# 这一层直接量：给每个登记的入口挂一个真闸门，调一次，看账本。
# **判据从"源码里有没有"变成"名额有没有被取走"。**


class _StubEmbedding(BaseEmbeddingModel):
    def _embed(self, data: str, /, **kwargs: Any) -> list[float]:
        return [0.0]

    async def _aembed(self, data: str, /, **kwargs: Any) -> list[float]:
        return [0.0]


class _StubMultimodal(MultimodalEmbeddingMixin, _StubEmbedding):
    def _embed_media(self, data: Any, /, **kwargs: Any) -> list[float]:
        return [0.0]

    async def _aembed_media(self, data: Any, /, **kwargs: Any) -> list[float]:
        return [0.0]


class _StubReranker(BaseReranker[str]):
    def _score(self, query: str, documents: Any, **kwargs: Any) -> list[float]:
        return [0.0] * len(list(documents))

    async def _ascore(self, query: str, documents: Any, **kwargs: Any) -> list[float]:
        return [0.0] * len(list(documents))


class _StubLoader(BaseLoader):
    def _load(self, source: Any, **kwargs: Any) -> LoaderContent:
        return LoaderContent(path=Path("/dev/null"), source=SourceContent("x"))

    async def _aload(self, source: Any, **kwargs: Any) -> LoaderContent:
        return self._load(source)

    def cleanup(self) -> None:
        return None


#: 每个登记入口调一次要花掉的名额数。绝大多数是 1；批量入口按来源数算。
#: 写死期望值而不是"大于 0"：**重复取用与完全不取一样是缺陷**（前者会死锁）。
INVOCATIONS: list[tuple[str, Any, int]] = [
    ("BaseEmbeddingModel.embed", lambda m: m.embed("x"), 1),
    ("BaseEmbeddingModel.embed_query", lambda m: m.embed_query("x"), 1),
    ("BaseEmbeddingModel.embed_document", lambda m: m.embed_document("x"), 1),
    ("BaseEmbeddingModel.embed_batch", lambda m: m.embed_batch(["x"]), 1),
    ("MultimodalEmbeddingMixin.embed_media", lambda m: m.embed_media("x"), 1),
    ("BaseReranker.score", lambda m: m.score("q", ["d"]), 1),
    ("BaseReranker.rank", lambda m: m.rank("q", ["d"]), 1),
    ("BaseLoader.load", lambda m: m.load("s"), 1),
    ("BaseLoader.batch_load", lambda m: m.batch_load(["a", "b", "c"], max_concurrency=2), 3),
]

_STUBS: dict[str, Any] = {
    "BaseEmbeddingModel": _StubEmbedding,
    "MultimodalEmbeddingMixin": _StubMultimodal,
    "BaseReranker": _StubReranker,
    "BaseLoader": _StubLoader,
}


@pytest.mark.parametrize(
    ("label", "invoke", "expected"), INVOCATIONS, ids=[i[0] for i in INVOCATIONS]
)
def test_sync_entry_points_really_take_a_permit(
    label: str, invoke: Any, expected: int
) -> None:
    """**量出来的，不是读出来的。**

    静态那层只能证明源码里出现了 `_through_gate_sync`。这条给入口挂上真闸门、
    调一次、看 `admitted` —— 少取是绕过了限流，多取是重复获取（会死锁，本轮
    在 `LocalLoader` 上实测过）。两者都是缺陷，所以期望值写死。
    """
    model = _STUBS[label.split(".")[0]]()
    gate = Gate(limit=8)
    model.bind_gate(gate)

    invoke(model)

    stats = gate.stats
    assert stats.admitted == expected, (
        f"{label} 取了 {stats.admitted} 次名额，期望 {expected} —— "
        f"少取是绕过限流，多取会死锁"
    )
    assert stats.in_flight == 0, f"{label} 结束后还有 {stats.in_flight} 个名额在外"


def test_behavioural_layer_covers_every_declared_direct_sync_entry() -> None:
    """**行为层不能落下任何一个静态层登记过的同步入口。**

    否则又回到"靠人记得给新入口补测试"—— 那正是这两层守卫要消灭的东西。
    """
    measured = {label for label, _, _ in INVOCATIONS}
    declared = {
        f"{cls.__name__}.{name}"
        for cls, groups in CLASSIFICATION.items()
        for name in groups["direct"] | groups["indirect"]
        if not name.startswith("a")  # 同步入口；异步侧另有并发用例覆盖
    }
    assert declared <= measured, (
        f"这些同步入口只做了静态检查，没有行为验证：{sorted(declared - measured)}"
    )
