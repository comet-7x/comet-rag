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
**每个公开方法都必须被显式归入下面四类之一**：

    direct    —— 自己进闸门（体内出现 `_through_gate*`）
    indirect  —— 经由某个 direct 方法进闸门（会核对它确实调了）
    delegated —— 路由器把请求交给另一个受闸门保护的资源
    exempt    —— 不发下游请求（关闭、清理这类）

新增一个公开方法却没归类，测试当场失败。**白名单没有"忘了新增的那个"这种
失效模式**，黑名单有 —— 这与 `test_layering.py` 里 engines 白名单是同一条理由。
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from comet_rag.core.concurrency import Gate
from comet_rag.engines.loaders.auto_loader import AutoLoader, LoaderRoute
from comet_rag.engines.loaders.base_loader import BaseLoader
from comet_rag.engines.loaders.local_loader import LocalLoader
from comet_rag.engines.loaders.types import LoaderContent, SourceContent
from comet_rag.engines.loaders.url_loader import URLLoader
from comet_rag.infrastructure.loaders.s3_loader import S3Loader
from comet_rag.infrastructure.providers.embedding.base import (
    BaseEmbeddingModel,
    MultimodalEmbeddingMixin,
)
from comet_rag.infrastructure.providers.embedding.openai_embedding_model import (
    OpenAIEmbeddingModel,
)
from comet_rag.infrastructure.providers.embedding.qwen3_vl_embedding import (
    DetokenizeResponse,
    EmbeddingData,
    Qwen3VLEmbeddingModel,
    TokenizeResponse,
)
from comet_rag.infrastructure.providers.reranker.base import BaseReranker
from comet_rag.infrastructure.providers.reranker.qwen3_vl_reranker import (
    Qwen3VLReranker,
)
from comet_rag.ports import MediaResource
from comet_rag.ports.gate import GatedResource

#: 四类的含义见模块文档。改动这张表时请连同理由一起写。
CLASSIFICATION: dict[type, dict[str, set[str]]] = {
    BaseEmbeddingModel: {
        "direct": {"embed", "embed_batch", "embed_document", "embed_query", "aembed", "aembed_batch"},
        # 走 `aembed`，由它进闸门
        "indirect": {"aembed_document", "aembed_query"},
        "delegated": set(),
        "exempt": {"aclose"},
    },
    MultimodalEmbeddingMixin: {
        "direct": {"embed_media", "aembed_media"},
        "indirect": set(),
        "delegated": set(),
        "exempt": set(),
    },
    BaseReranker: {
        "direct": {"score", "ascore"},
        # 翻译成供应商格式之后走 `score`/`ascore`
        "indirect": {"rank", "arank"},
        "delegated": set(),
        "exempt": {"aclose"},
    },
    BaseLoader: {
        "direct": {"load", "aload"},
        # 每个来源各走一次 `load`/`aload`，所以是每次请求一个名额
        "indirect": {"batch_load", "abatch_load"},
        "delegated": set(),
        "exempt": {"cleanup", "acleanup"},
    },
    Qwen3VLEmbeddingModel: {
        # 供应商专属 token API 同样会发 HTTP 请求，不能躲在具体类里绕过预算。
        "direct": {"tokenize", "atokenize", "detokenize", "adetokenize"},
        "indirect": {
            "embed_image",
            "aembed_image",
            "embed_content",
            "aembed_content",
            "get_output_dim",
            "get_max_model_len",
        },
        "delegated": set(),
        "exempt": {"aclose"},
    },
    URLLoader: {
        # 为了复用 client，批量实现绕过 BaseLoader.batch_load，必须在这里自己进闸。
        "direct": {"abatch_load"},
        "indirect": set(),
        # 同步逐请求闸门位于 `_batch_load_with` 的 worker 中。
        "delegated": {"batch_load"},
        "exempt": {"cleanup", "acleanup", "aclose"},
    },
    AutoLoader: {
        "direct": set(),
        "indirect": set(),
        # 路由器自己不持有闸门；名额由匹配到的叶子 loader 获取。
        "delegated": {"batch_load", "abatch_load"},
        "exempt": {
            "bind_gate",
            "cleanup",
            "acleanup",
            "aclose",
            "default",
            "default_routes",
        },
    },
    LocalLoader: {
        "direct": set(),
        "indirect": set(),
        "delegated": set(),
        "exempt": {"cleanup"},
    },
    S3Loader: {
        "direct": set(),
        "indirect": set(),
        "delegated": set(),
        "exempt": {"cleanup", "acleanup", "aclose"},
    },
    OpenAIEmbeddingModel: {
        "direct": set(),
        "indirect": set(),
        "delegated": set(),
        "exempt": {"aclose"},
    },
    Qwen3VLReranker: {
        "direct": set(),
        "indirect": set(),
        "delegated": set(),
        "exempt": {"aclose"},
    },
}

#: delegated 不能只凭分类表口头承诺；每个入口都要指出真实委派目标。
DELEGATION_TARGETS: dict[tuple[type, str], str] = {
    (URLLoader, "batch_load"): "_batch_load_with",
    (AutoLoader, "batch_load"): "batch_load",
    (AutoLoader, "abatch_load"): "abatch_load",
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


def _all_attributes(method: Any) -> set[str]:
    """方法体里所有属性访问；用于验证路由器确实委派给另一个公开入口。"""
    source = textwrap.dedent(inspect.getsource(method))
    return {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
    }


def _public_methods(cls: type) -> dict[str, Any]:
    """类**自己定义**的公开方法。继承自 `GatedResource` 的不算。"""
    methods: dict[str, Any] = {}
    for name, value in vars(cls).items():
        if name.startswith("_"):
            continue
        method = value.__func__ if isinstance(value, (classmethod, staticmethod)) else value
        if callable(method):
            methods[name] = method
    return methods


def _ast_base_name(node: ast.expr, bindings: dict[str, str]) -> str | None:
    """Resolve a base expression against the bindings in its lexical scope."""
    while isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        root = bindings.get(node.id, node.id)
        return ".".join((root, *reversed(parts)))
    return None


def _nested_statement_blocks(node: ast.stmt) -> Iterator[list[ast.stmt]]:
    """Yield child statement blocks without leaking bindings between branches."""
    if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        yield node.body
        yield node.orelse
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        yield node.body
    elif isinstance(node, (ast.Try, ast.TryStar)):
        yield node.body
        yield node.orelse
        yield node.finalbody
        for handler in node.handlers:
            yield handler.body
    elif isinstance(node, ast.Match):
        for case in node.cases:
            yield case.body


def _import_from_module(current_module: str, item: ast.ImportFrom) -> str:
    """Resolve the module portion of an absolute or relative import."""
    if item.level == 0:
        return item.module or ""
    package = current_module.rpartition(".")[0].split(".")
    keep = max(0, len(package) - item.level + 1)
    suffix = item.module.split(".") if item.module else []
    return ".".join((*package[:keep], *suffix))


def _scoped_classes(
    statements: Iterable[ast.stmt],
    module_name: str,
    scope: tuple[str, ...] = (),
    inherited_bindings: dict[str, str] | None = None,
    function_bindings: dict[str, str] | None = None,
) -> Iterator[tuple[str, ast.ClassDef, set[str]]]:
    """Yield classes and resolved bases using lexical import bindings."""
    bindings = dict(inherited_bindings or {})
    for item in statements:
        if isinstance(item, ast.ImportFrom):
            imported_module = _import_from_module(module_name, item)
            for alias in item.names:
                if alias.name == "*":
                    continue
                local_name = alias.asname or alias.name
                bindings[local_name] = ".".join((imported_module, alias.name))
            continue
        if isinstance(item, ast.Import):
            for alias in item.names:
                local_name = alias.asname or alias.name.partition(".")[0]
                bindings[local_name] = alias.name if alias.asname else local_name
            continue
        if isinstance(item, ast.ClassDef):
            class_scope = (*scope, item.name)
            qualified_name = ".".join((module_name, *class_scope))
            bases = {
                name
                for base in item.bases
                if (name := _ast_base_name(base, bindings)) is not None
            }
            yield qualified_name, item, bases
            method_bindings = bindings if function_bindings is None else function_bindings
            yield from _scoped_classes(
                item.body,
                module_name,
                class_scope,
                bindings,
                method_bindings,
            )
            bindings[item.name] = qualified_name
            continue
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inherited = bindings if function_bindings is None else function_bindings
            yield from _scoped_classes(
                item.body,
                module_name,
                (*scope, item.name, "<locals>"),
                inherited,
            )
            continue
        for block in _nested_statement_blocks(item):
            yield from _scoped_classes(
                block,
                module_name,
                scope,
                bindings,
                function_bindings,
            )


def _gated_classes_from_modules(
    modules: Iterable[tuple[str, ast.Module]],
) -> set[str]:
    """Build the complete gated inheritance graph from parsed modules."""
    bases_by_class: dict[str, set[str]] = {}
    classes_with_public_methods: set[str] = set()

    for module_name, module in modules:
        for qualified_name, item, bases in _scoped_classes(module.body, module_name):
            bases_by_class[qualified_name] = bases
            if any(
                isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not member.name.startswith("_")
                for member in item.body
            ):
                classes_with_public_methods.add(qualified_name)

    root_name = f"{GatedResource.__module__}.{GatedResource.__qualname__}"
    descendants = {root_name}
    while newly_found := {
        qualified_name
        for qualified_name, bases in bases_by_class.items()
        if qualified_name not in descendants and bases & descendants
    }:
        descendants.update(newly_found)

    return (descendants - {root_name}) & classes_with_public_methods


def _production_gated_classes() -> set[str]:
    """Discover every production ``GatedResource`` descendant from source.

    Runtime ``__subclasses__()`` only sees modules already imported by this test.
    Scanning the package AST makes an implementation in a newly added, otherwise
    unimported module visible to the guard as well.
    """
    package_root = Path(__file__).parents[2] / "comet_rag"

    def parsed_modules() -> Iterator[tuple[str, ast.Module]]:
        for path in package_root.rglob("*.py"):
            module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            module_parts = path.relative_to(package_root.parent).with_suffix("").parts
            if module_parts[-1] == "__init__":
                module_parts = module_parts[:-1]
            yield ".".join(module_parts), module

    return _gated_classes_from_modules(parsed_modules())


def _direct_entry_names(cls: type) -> set[str]:
    """当前类及其基类声明的所有 direct 入口。"""
    return {
        name
        for base in cls.__mro__
        if base in CLASSIFICATION
        for name in CLASSIFICATION[base]["direct"]
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
    direct = _direct_entry_names(cls)
    for name in sorted(CLASSIFICATION[cls]["indirect"]):
        method = _public_methods(cls)[name]
        assert _self_attributes(method) & direct, (
            f"{cls.__name__}.{name} 归类为 indirect，但体内没有调用任何 direct 入口"
            f"（{sorted(direct)}）—— 它可能根本没经过闸门"
        )


@pytest.mark.parametrize("cls", list(CLASSIFICATION), ids=lambda c: c.__name__)
def test_delegated_entries_really_call_another_request_entry(cls: type) -> None:
    """delegated 入口必须把工作交给另一个已登记的请求入口。"""
    for name in sorted(CLASSIFICATION[cls]["delegated"]):
        method = _public_methods(cls)[name]
        target = DELEGATION_TARGETS[(cls, name)]
        assert target in _all_attributes(method), (
            f"{cls.__name__}.{name} 归类为 delegated，但没有调用约定目标 {target}"
        )


def test_every_delegated_entry_declares_its_target() -> None:
    declared = {
        (cls, name)
        for cls, groups in CLASSIFICATION.items()
        for name in groups["delegated"]
    }
    assert declared == set(DELEGATION_TARGETS)


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
    """**抽象基类和具体适配器都必须进入这张表。**

    旧实现只检查抽象类，因此 Qwen 在具体类上新增四个公开 HTTP 入口时，整组
    守卫完全看不见。只要生产类自己声明了公开方法，就必须逐个分类。
    """
    discovered = _production_gated_classes()
    classified = {f"{cls.__module__}.{cls.__qualname__}" for cls in CLASSIFICATION}
    assert discovered == classified, (
        f"生产源码与分类表不一致；未登记：{sorted(discovered - classified)}；"
        f"已失效：{sorted(classified - discovered)}"
    )


def test_ast_discovery_covers_non_top_level_gated_classes() -> None:
    """发现器不能遗漏非顶层类，也不能混淆不同作用域里的同名别名。"""
    module = ast.parse(
        """
from comet_rag.ports.gate import GatedResource as GateBase

if TYPE_CHECKING:
    class Conditional(GateBase):
        def request(self): ...

class Outer:
    class Nested(GateBase):
        async def request(self): ...

def factory():
    class Local(GateBase):
        def request(self): ...

def gated_factory():
    from comet_rag.ports.gate import GatedResource as Resource

    class GatedLocal(Resource):
        def request(self): ...

def other_factory():
    from other_module import OtherBase as Resource

    class Unrelated(Resource):
        def request(self): ...

class ClassScopeDoesNotEncloseMethods:
    from comet_rag.ports.gate import GatedResource as ClassResource

    def factory():
        class AlsoUnrelated(ClassResource):
            def request(self): ...

class MethodFactory:
    def factory():
        from comet_rag.ports.gate import GatedResource as MethodResource

        class MethodLocal(MethodResource):
            def request(self): ...
"""
    )

    assert _gated_classes_from_modules([("comet_rag.synthetic", module)]) == {
        "comet_rag.synthetic.Conditional",
        "comet_rag.synthetic.Outer.Nested",
        "comet_rag.synthetic.factory.<locals>.Local",
        "comet_rag.synthetic.gated_factory.<locals>.GatedLocal",
        "comet_rag.synthetic.MethodFactory.factory.<locals>.MethodLocal",
    }


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


class _StubQwen(Qwen3VLEmbeddingModel):
    """不建 HTTP client，只验证 Qwen 公开外壳的闸门记账。"""

    def __init__(self) -> None:
        self._output_dim: int | None = None
        self._max_model_len: int | None = None

    def _embed(
        self,
        embedding_data: Any,
        system_prompt: Any = None,
        encoding_format: Any = None,
        continue_final_message: bool = True,
        add_special_tokens: bool = True,
        **kwargs: Any,
    ) -> list[float]:
        return [0.0]

    async def _aembed(
        self,
        embedding_data: Any,
        system_prompt: Any = None,
        encoding_format: Any = None,
        continue_final_message: bool = True,
        add_special_tokens: bool = True,
        **kwargs: Any,
    ) -> list[float]:
        return [0.0]

    def _embed_media(self, data: Any, /, **kwargs: Any) -> list[float]:
        return [0.0]

    async def _aembed_media(self, data: Any, /, **kwargs: Any) -> list[float]:
        return [0.0]

    def _tokenize(
        self,
        embedding_data: EmbeddingData,
        continue_final_message: bool = False,
        return_token_strs: bool = False,
        **kwargs: Any,
    ) -> TokenizeResponse:
        return TokenizeResponse(count=1, max_model_len=1024, tokens=[1])

    async def _atokenize(
        self,
        embedding_data: EmbeddingData,
        continue_final_message: bool = False,
        return_token_strs: bool = False,
        **kwargs: Any,
    ) -> TokenizeResponse:
        return TokenizeResponse(count=1, max_model_len=1024, tokens=[1])

    def _detokenize(self, tokens: list[int], **kwargs: Any) -> DetokenizeResponse:
        return DetokenizeResponse(prompt="x")

    async def _adetokenize(
        self, tokens: list[int], **kwargs: Any
    ) -> DetokenizeResponse:
        return DetokenizeResponse(prompt="x")


class _StubURLLoader(URLLoader):
    """保留 URLLoader 的批量外壳，但把真实下载替换成内存结果。"""

    def _shared_client(self) -> Any:
        return object()

    def _shared_async_client(self) -> Any:
        return object()

    def _load_impl(
        self, source: SourceContent | str, *, download_config: Any = None, client: Any = None
    ) -> LoaderContent:
        return LoaderContent(path=Path("/dev/null"), source=SourceContent("x"))

    async def _aload_impl(
        self, source: SourceContent | str, *, download_config: Any = None, client: Any = None
    ) -> LoaderContent:
        return LoaderContent(path=Path("/dev/null"), source=SourceContent("x"))


def _stub_auto_loader() -> AutoLoader:
    return AutoLoader(
        [LoaderRoute(name="all", matcher=lambda _source: True, loader=_StubLoader())]
    )


#: 每个登记入口调一次要花掉的名额数。绝大多数是 1；批量入口按来源数算。
#: 写死期望值而不是"大于 0"：**重复取用与完全不取一样是缺陷**（前者会死锁）。
SYNC_INVOCATIONS: list[tuple[str, Any, int]] = [
    ("BaseEmbeddingModel.embed", lambda m: m.embed("x"), 1),
    ("BaseEmbeddingModel.embed_query", lambda m: m.embed_query("x"), 1),
    ("BaseEmbeddingModel.embed_document", lambda m: m.embed_document("x"), 1),
    ("BaseEmbeddingModel.embed_batch", lambda m: m.embed_batch(["x"]), 1),
    ("MultimodalEmbeddingMixin.embed_media", lambda m: m.embed_media("x"), 1),
    ("BaseReranker.score", lambda m: m.score("q", ["d"]), 1),
    ("BaseReranker.rank", lambda m: m.rank("q", ["d"]), 1),
    ("BaseLoader.load", lambda m: m.load("s"), 1),
    ("BaseLoader.batch_load", lambda m: m.batch_load(["a", "b", "c"], max_concurrency=2), 3),
    ("Qwen3VLEmbeddingModel.tokenize", lambda m: m.tokenize(EmbeddingData(text="x")), 1),
    ("Qwen3VLEmbeddingModel.detokenize", lambda m: m.detokenize([1]), 1),
    (
        "Qwen3VLEmbeddingModel.embed_image",
        lambda m: m.embed_image(MediaResource(data=b"x", mimetype="image/png")),
        1,
    ),
    ("Qwen3VLEmbeddingModel.embed_content", lambda m: m.embed_content("x"), 1),
    ("Qwen3VLEmbeddingModel.get_output_dim", lambda m: m.get_output_dim(), 1),
    ("Qwen3VLEmbeddingModel.get_max_model_len", lambda m: m.get_max_model_len(), 1),
    ("URLLoader.batch_load", lambda m: m.batch_load(["a", "b", "c"], max_concurrency=2), 3),
    ("AutoLoader.batch_load", lambda m: m.batch_load(["a", "b", "c"], max_concurrency=2), 3),
]

#: 异步入口同样要量。第一版把 `a` 开头的排除在完整性检查外，理由写的是
#: "异步侧另有并发用例覆盖" —— 那些用例确实存在，但它们是**零散写的**，
#: 不由这张表驱动。于是"守卫覆盖了每个登记入口"这句话当时是假的：新加一个
#: 异步入口就会零行为覆盖，而元守卫不会吭声（评审指出）。
ASYNC_INVOCATIONS: list[tuple[str, Any, int]] = [
    ("BaseEmbeddingModel.aembed", lambda m: m.aembed("x"), 1),
    ("BaseEmbeddingModel.aembed_query", lambda m: m.aembed_query("x"), 1),
    ("BaseEmbeddingModel.aembed_document", lambda m: m.aembed_document("x"), 1),
    ("BaseEmbeddingModel.aembed_batch", lambda m: m.aembed_batch(["x"]), 1),
    ("MultimodalEmbeddingMixin.aembed_media", lambda m: m.aembed_media("x"), 1),
    ("BaseReranker.ascore", lambda m: m.ascore("q", ["d"]), 1),
    ("BaseReranker.arank", lambda m: m.arank("q", ["d"]), 1),
    ("BaseLoader.aload", lambda m: m.aload("s"), 1),
    (
        "BaseLoader.abatch_load",
        lambda m: m.abatch_load(["a", "b", "c"], max_concurrency=2),
        3,
    ),
    (
        "Qwen3VLEmbeddingModel.atokenize",
        lambda m: m.atokenize(EmbeddingData(text="x")),
        1,
    ),
    ("Qwen3VLEmbeddingModel.adetokenize", lambda m: m.adetokenize([1]), 1),
    (
        "Qwen3VLEmbeddingModel.aembed_image",
        lambda m: m.aembed_image(MediaResource(data=b"x", mimetype="image/png")),
        1,
    ),
    ("Qwen3VLEmbeddingModel.aembed_content", lambda m: m.aembed_content("x"), 1),
    (
        "URLLoader.abatch_load",
        lambda m: m.abatch_load(["a", "b", "c"], max_concurrency=2),
        3,
    ),
    (
        "AutoLoader.abatch_load",
        lambda m: m.abatch_load(["a", "b", "c"], max_concurrency=2),
        3,
    ),
]

_STUBS: dict[str, Any] = {
    "BaseEmbeddingModel": _StubEmbedding,
    "MultimodalEmbeddingMixin": _StubMultimodal,
    "BaseReranker": _StubReranker,
    "BaseLoader": _StubLoader,
    "Qwen3VLEmbeddingModel": _StubQwen,
    "URLLoader": _StubURLLoader,
    "AutoLoader": _stub_auto_loader,
}


@pytest.mark.parametrize(
    ("label", "invoke", "expected"),
    SYNC_INVOCATIONS,
    ids=[i[0] for i in SYNC_INVOCATIONS],
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


@pytest.mark.parametrize(
    ("label", "invoke", "expected"),
    ASYNC_INVOCATIONS,
    ids=[i[0] for i in ASYNC_INVOCATIONS],
)
async def test_async_entry_points_really_take_a_permit(
    label: str, invoke: Any, expected: int
) -> None:
    """异步侧同理。静态层同样只能证明源码里出现过 `_through_gate`。"""
    model = _STUBS[label.split(".")[0]]()
    gate = Gate(limit=8)
    model.bind_gate(gate)

    await invoke(model)

    stats = gate.stats
    assert stats.admitted == expected, (
        f"{label} 取了 {stats.admitted} 次名额，期望 {expected} —— "
        f"少取是绕过限流，多取会死锁"
    )
    assert stats.in_flight == 0, f"{label} 结束后还有 {stats.in_flight} 个名额在外"


def test_behavioural_layer_covers_every_declared_entry() -> None:
    """**行为层不能落下任何一个登记入口，同步异步都算。**

    否则又回到"靠人记得给新入口补测试"—— 那正是这两层守卫要消灭的东西。
    第一版把异步排除在外，于是这句话当时是假的。
    """
    measured = {label for label, _, _ in [*SYNC_INVOCATIONS, *ASYNC_INVOCATIONS]}
    declared = {
        f"{cls.__name__}.{name}"
        for cls, groups in CLASSIFICATION.items()
        for name in groups["direct"] | groups["indirect"] | groups["delegated"]
    }
    assert declared <= measured, (
        f"这些入口只做了静态检查，没有行为验证：{sorted(declared - measured)}"
    )
