"""文档防腐守卫。

`docs/pipeline_usage.md` 曾整体落后于代码一个重构：hook 签名少一个参数、
调用了并不存在的 `clean_to_string`、import 路径少一层 `models`。
照抄文档的示例代码会直接报错 —— 对开源项目来说这是第一批用户的第一印象。

本测试不执行依赖真实文件或远程服务的示例，检查语法、项目符号、真实调用签名，
以及能可靠解析的直接调用是否等待了异步方法、是否混用了两种模式的注入对象。
复杂别名、跨代码块变量与完整实参类型推导仍不在这个守卫的覆盖范围内。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = PROJECT_ROOT / "docs"

_CODE_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _iter_docs() -> list[Path]:
    """递归 docs/**/*.md **加上 README** —— README 是第一批用户看到的第一段代码，
    却曾经不在守卫范围内。补进来的当天就抓到一个错的示例：
    `for chunk in pipeline.run(...)`，而 `run()` 返回的是 `PipelineResult`，
    真正可迭代的是 `result.chunks`。"""
    return [PROJECT_ROOT / "README.md", *sorted(DOCS_DIR.rglob("*.md"))]


def _iter_blocks() -> list[tuple[str, int, str]]:
    """返回 (文档名, 块序号, 源码) 三元组。"""
    blocks: list[tuple[str, int, str]] = []
    for doc in _iter_docs():
        if not doc.exists():  # pragma: no cover
            continue
        text = doc.read_text(encoding="utf-8")
        for i, match in enumerate(_CODE_BLOCK.finditer(text)):
            blocks.append((doc.name, i, match.group(1)))
    return blocks


BLOCKS = _iter_blocks()


def test_docs_contain_examples() -> None:
    """若某次改动把所有示例删光，上面的参数化会静默变成零用例。"""
    assert BLOCKS, "docs/ 下没有找到任何 python 代码块，检查提取正则"


@pytest.mark.parametrize(
    ("doc", "index", "source"), BLOCKS, ids=[f"{d}#{i}" for d, i, _ in BLOCKS]
)
def test_example_parses(doc: str, index: int, source: str) -> None:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        pytest.fail(f"{doc} 第 {index + 1} 个代码块语法错误：{exc}")


@pytest.mark.parametrize(
    ("doc", "index", "source"), BLOCKS, ids=[f"{d}#{i}" for d, i, _ in BLOCKS]
)
def test_example_imports_resolve(doc: str, index: int, source: str) -> None:
    """校验示例里引用的 comet_rag 符号真实存在。

    只检查 comet_rag 自身的导入 —— 第三方包是否安装取决于装了哪些 extras，
    不该让文档测试依赖那个。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith("comet_rag"):
            continue

        try:
            module = importlib.import_module(node.module)
        except ImportError as exc:
            pytest.fail(
                f"{doc} 第 {index + 1} 个代码块的模块不存在：{node.module}（{exc}）"
            )

        for alias in node.names:
            assert hasattr(module, alias.name), (
                f"{doc} 第 {index + 1} 个代码块引用了不存在的符号："
                f"{node.module}.{alias.name}"
            )


# ── 第三层：调用签名 ───────────────────────────────────────────────────────
#
# 前两层（语法、符号存在）都拦不住"符号在、参数不对"。实际发生过：模板方法
# 重构把 `URLLoader.load` 的签名收窄成只收 `source`，而 docs/pipeline_usage.md
# 明确教用户 `URLLoader.load(url, download_config=...)` —— 照抄当场 TypeError，
# 而且**连过两轮 bot 评审都没被发现**，因为符号确实存在。
#
# ## 这一层抓不到什么（写下来，免得它给人虚假的安全感）
#
# 1. **散文里的用法。** 只有 ```python 代码块会被检查。上面那个回归最初就漏在
#    这里 —— 文档是用一句话描述该怎么调的。所以"值得承诺的用法就写成可执行
#    示例"不是文风偏好，而是它能不能被守住的前提。
#
# 2. **`**kwargs` 吞掉的拼写错误。** `embed_documents(..., max_concurency=8)`
#    能正常绑定，因为签名里有 `**kwargs`。这不是缺陷而是语义：那类 API 本来就
#    接受任意关键字。想让拼写错误暴露，得让 API 自己拒绝未知参数
#    （`BaseLoader._reject_unsupported` 就是这么做的）。
#
# 3. **类型。** 只检查"能不能绑上"，不校验实参类型 —— 那需要真正的类型推导，
#    留给 pyright。


class _Placeholder:
    """占位实参。只校验能不能绑上，不关心值。"""


def _resolve_symbols(tree: ast.AST) -> dict[str, Any]:
    """示例里从 comet_rag 导入进来的名字 → 真实对象。"""
    symbols: dict[str, Any] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith("comet_rag"):
            continue
        try:
            module = importlib.import_module(node.module)
        except ImportError:  # pragma: no cover - 由上一层用例负责报错
            continue
        for alias in node.names:
            target = getattr(module, alias.name, None)
            if target is not None:
                symbols[alias.asname or alias.name] = target
    return symbols


def _constructed_type(node: ast.expr, symbols: dict[str, Any]) -> type | None:
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        value = symbols.get(node.func.id)
        return value if isinstance(value, type) else None
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        value = symbols.get(node.func.value.id)
        if node.func.attr == "default" and isinstance(value, type):
            return value
        if node.func.attr == "fork" and value is not None:
            return type(value)
    return None


def _instance_types(tree: ast.AST, symbols: dict[str, Any]) -> dict[str, type]:
    """识别直接构造、默认工厂与 with 绑定；不猜测复杂表达式的返回类型。"""
    types: dict[str, type] = {}
    for node in ast.walk(tree):
        bindings: list[tuple[ast.expr, ast.expr]] = []
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            bindings.append((node.targets[0], node.value))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            bindings.extend(
                (item.optional_vars, item.context_expr)
                for item in node.items
                if item.optional_vars is not None
            )
        for target, value in bindings:
            constructed = _constructed_type(value, symbols)
            if isinstance(target, ast.Name) and constructed is not None:
                types[target.id] = constructed
    return types


def _callable_for(
    node: ast.Call, symbols: dict[str, Any], types: dict[str, type]
) -> tuple[str, Any, bool] | None:
    """把调用点解析成 (显示名, 可调用对象, 是否需要补 self)。

    第三项不能省：从**类**上取到的普通方法是未绑定函数，签名里带 `self`，
    而文档里写的是实例调用。忘了补，每一处实例方法调用都会被误报成"少传
    一个参数" —— 这个守卫第一版就是这么写的，一上来报了 7 条全是假的。
    误报比漏报更致命：它会让人开始无视这个测试。
    """
    func = node.func
    if isinstance(func, ast.Name):
        target = symbols.get(func.id)
        return (func.id, target, False) if callable(target) else None
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        owner = types.get(func.value.id)
        is_instance_call = owner is not None
        if owner is None:
            owner = symbols.get(func.value.id)
        if owner is None:
            return None
        label = owner.__name__ if isinstance(owner, type) else type(owner).__name__
        assert hasattr(owner, func.attr), f"{label} 没有方法 {func.attr}"
        method = getattr(owner, func.attr)
        if not callable(method):
            return None
        raw = inspect.getattr_static(owner, func.attr)
        needs_self = (
            is_instance_call
            and not inspect.ismethod(method)
            and not isinstance(raw, staticmethod)
        )
        return (f"{label}.{func.attr}", method, needs_self)
    return None


@pytest.mark.parametrize(
    ("doc", "index", "source"), BLOCKS, ids=[f"{d}#{i}" for d, i, _ in BLOCKS]
)
def test_example_calls_match_real_signatures(doc: str, index: int, source: str) -> None:
    """文档里的调用必须能真的绑上目标签名。

    只做**能否绑定**的检查（`Signature.bind`），不校验类型 —— 参数名拼错、
    少传、多传都会被抓到，而这正是文档腐化最常见的三种形态。
    """
    tree = ast.parse(source)
    symbols = _resolve_symbols(tree)
    if not symbols:
        pytest.skip("该代码块没有引用 comet_rag 的符号")
    types = _instance_types(tree, symbols)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # 带 *args / **kwargs 解包的调用无法静态判定实参个数，跳过
        if any(isinstance(a, ast.Starred) for a in node.args) or any(
            kw.arg is None for kw in node.keywords
        ):
            continue
        resolved = _callable_for(node, symbols, types)
        if resolved is None:
            continue
        label, target, needs_self = resolved
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):  # pragma: no cover - 内建/C 实现
            continue

        positional = [_Placeholder()] * (len(node.args) + int(needs_self))
        keywords = {kw.arg: _Placeholder() for kw in node.keywords if kw.arg}
        try:
            signature.bind(*positional, **keywords)
        except TypeError as exc:
            pytest.fail(
                f"{doc} 第 {index + 1} 个代码块的调用与真实签名不符："
                f"{label}({', '.join([*['…'] * len(positional), *keywords])}) —— {exc}。"
                f"实际签名：{label}{signature}"
            )


# 同名方法无法靠 Signature.bind 分辨同步/异步；这里只检查能可靠解析的直接调用。
def _mode(name: str) -> bool | None:
    if name.startswith("Async"):
        return True
    if name in {
        "Pipeline",
        "HookRegistry",
        "OpenAIEmbeddingModel",
        "Qwen3VLEmbeddingModel",
        "Qwen3VLReranker",
        "OpenAIVisionModel",
        "DocxCleaner",
        "DocxDocumentExtractor",
        "MinerUDocumentExtractor",
        "AutoLoader",
        "URLLoader",
        "LocalLoader",
        "S3Loader",
        "Client",
        "OpenAI",
    }:
        return False
    return None


def _value_mode(
    node: ast.expr,
    symbols: dict[str, Any],
    types: dict[str, type],
    clients: dict[str, bool],
) -> bool | None:
    if isinstance(node, ast.Name):
        if node.id in clients:
            return clients[node.id]
        target = types.get(node.id) or symbols.get(node.id)
        if target is not None:
            return _mode(
                target.__name__ if isinstance(target, type) else type(target).__name__
            )
    if isinstance(node, ast.Call):
        constructed = _constructed_type(node, symbols)
        if constructed is not None:
            return _mode(constructed.__name__)
        if isinstance(node.func, ast.Attribute):
            return _mode(node.func.attr)
        if isinstance(node.func, ast.Name):
            return _mode(node.func.id)
    return None


def _execution_mode_errors(source: str) -> list[str]:
    tree = ast.parse(source)
    symbols = _resolve_symbols(tree)
    types = _instance_types(tree, symbols)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    clients: dict[str, bool] = {}
    for node in ast.walk(tree):
        bindings: list[tuple[ast.expr, ast.expr]] = []
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            bindings.append((node.targets[0], node.value))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            bindings.extend(
                (item.optional_vars, item.context_expr)
                for item in node.items
                if item.optional_vars is not None
            )
        for name, value in bindings:
            mode = _value_mode(value, symbols, types, clients)
            if isinstance(name, ast.Name) and mode is not None:
                clients[name.id] = mode

    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _callable_for(node, symbols, types)
        if resolved is None:
            continue
        label, target, _ = resolved
        parent = parents.get(node)
        awaited = isinstance(parent, ast.Await)
        # 明确交给 asyncio 调度的 coroutine 由调度器等待，不要求调用处 await。
        scheduled = (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Attribute)
            and isinstance(parent.func.value, ast.Name)
            and parent.func.value.id == "asyncio"
            and parent.func.attr in {"run", "create_task", "gather"}
        )
        if inspect.iscoroutinefunction(target) and not awaited and not scheduled:
            errors.append(f"{label} 缺少 await")
        if awaited and not inspect.iscoroutinefunction(target):
            errors.append(f"{label} 是同步调用或异步生成器，不能 await")
        if isinstance(target, type):
            mode = _mode(target.__name__)
            if mode is None:
                continue
            for keyword in node.keywords:
                if keyword.arg not in {
                    "client",
                    "embedding_model",
                    "loader",
                    "hooks",
                    "vision_model",
                }:
                    continue
                value_mode = _value_mode(keyword.value, symbols, types, clients)
                if value_mode is not None and value_mode != mode:
                    errors.append(f"{label} 的 {keyword.arg} 执行模式不匹配")
    return errors


@pytest.mark.parametrize(
    ("doc", "index", "source"), BLOCKS, ids=[f"{d}#{i}" for d, i, _ in BLOCKS]
)
def test_example_execution_mode_matches_real_api(
    doc: str, index: int, source: str
) -> None:
    assert not (errors := _execution_mode_errors(source)), (
        f"{doc} 第 {index + 1} 块：{errors}"
    )
