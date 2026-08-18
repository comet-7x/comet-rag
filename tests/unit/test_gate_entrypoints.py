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
from typing import Any

import pytest

from comet_rag.engines.loaders.base_loader import BaseLoader
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
