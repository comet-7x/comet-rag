"""架构分层守卫。

强制执行 spec A1：`engines/` 不得依赖任何基础设施。

这条约束是"库 + 参考服务"双重定位的支点 —— 一旦 engines 里出现 `import sqlalchemy`，
用户为了跑一个 docx 解析器就得装一整套中间件，"库"这一半当场作废。

用 AST 而非 grep：注释、字符串、文档里出现 "import redis" 不该误报，
而 `if TYPE_CHECKING: import x` 这类真实导入不该漏报。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENGINES = PROJECT_ROOT / "comet_rag" / "engines"

# 基础设施包白名单之外的一切。命中即违反 A1。
FORBIDDEN_IN_ENGINES = frozenset(
    {
        "redis",
        "pymilvus",
        "sqlalchemy",
        "alembic",
        "arq",
        "fastapi",
        "starlette",
        "uvicorn",
        "aioboto3",
        "boto3",
        "asyncpg",
        "psycopg",
        "pymongo",
        "motor",
        "celery",
    }
)

# engines 不得反向依赖上层
FORBIDDEN_INTERNAL = ("comet_rag.api", "comet_rag.workers", "comet_rag.services")


def _iter_engine_modules() -> list[Path]:
    return sorted(p for p in ENGINES.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_roots(tree: ast.AST) -> set[str]:
    """收集模块中所有导入的顶层包名（含函数内的延迟导入）。"""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        # level > 0 是相对导入，不涉及第三方包
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_full(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", _iter_engine_modules(), ids=lambda p: p.name)
def test_engines_do_not_import_infrastructure(module: Path) -> None:
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    violations = _imported_roots(tree) & FORBIDDEN_IN_ENGINES
    assert not violations, (
        f"{module.relative_to(PROJECT_ROOT)} 违反 spec A1，导入了基础设施包：{sorted(violations)}。"
        f"若确需此能力，应放到 infrastructure/ 并通过接口注入。"
    )


@pytest.mark.parametrize("module", _iter_engine_modules(), ids=lambda p: p.name)
def test_engines_do_not_import_upper_layers(module: Path) -> None:
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    violations = {
        name for name in _imported_full(tree) if name.startswith(FORBIDDEN_INTERNAL)
    }
    assert not violations, (
        f"{module.relative_to(PROJECT_ROOT)} 反向依赖了上层：{sorted(violations)}。"
        f"依赖方向必须是 api/workers → services → engines，不可反向。"
    )


def test_guard_actually_detects_violations() -> None:
    """守卫自检：确保上面两个测试不是永远为真。"""
    tree = ast.parse("import sqlalchemy\nfrom comet_rag.api import deps\n")
    assert _imported_roots(tree) & FORBIDDEN_IN_ENGINES == {"sqlalchemy"}
    assert any(n.startswith(FORBIDDEN_INTERNAL) for n in _imported_full(tree))


# ── 业务/引擎依赖模型 Port，而不是供应商适配器 ─────────────────────────────

MODEL_ADAPTER_PACKAGE = "comet_rag.infrastructure.models"


def _model_port_consumers() -> list[Path]:
    root = PROJECT_ROOT / "comet_rag"
    return sorted(
        path
        for package in ("services", "engines")
        for path in (root / package).rglob("*.py")
        if "__pycache__" not in path.parts
    )


@pytest.mark.parametrize("module", _model_port_consumers(), ids=lambda p: p.name)
def test_business_code_depends_on_model_ports(module: Path) -> None:
    """供应商模型只能在组合根选择，不能泄漏到业务和纯计算模块。"""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    hits = {
        name
        for name in _imported_full(tree)
        if name.startswith(MODEL_ADAPTER_PACKAGE)
    }
    assert not hits, (
        f"{module.relative_to(PROJECT_ROOT)} 直接依赖了模型适配器：{sorted(hits)}。"
        "请依赖 comet_rag.application.ports，并在 core/bootstrap.py 装配实现。"
    )


# ── 单进程模式不得挂上租约回收（T24）────────────────────────────────────────

#: 除了 workers/ 自己，谁都不该 import 它。写成路径前缀，子模块一并覆盖。
MAINTENANCE = "comet_rag.workers.maintenance"

#: 单进程部署会加载的东西：组合根、API、以及任务框架本身
SINGLE_PROCESS_TREES = ("core", "api", "tasks", "services", "engines", "infrastructure")


def _single_process_modules() -> list[Path]:
    root = PROJECT_ROOT / "comet_rag"
    return sorted(
        p
        for tree in SINGLE_PROCESS_TREES
        for p in (root / tree).rglob("*.py")
        if "__pycache__" not in p.parts
    )


@pytest.mark.parametrize("module", _single_process_modules(), ids=lambda p: p.name)
def test_single_process_paths_never_pull_in_lease_reclaim(module: Path) -> None:
    """**单进程模式绝不能启用租约回收**（T24 的验收标准之一）。

    那时任务的协程还活在本进程里，只是心跳没来得及写。回收会把它退回队列
    让别人再跑一遍 —— "一份任务两个执行者"，而且两边都不知道对方存在。

    保证方式不是加一个开关（开关会被配错），而是**结构上够不着**：
    `sweep_cron` 只在 `workers/` 下注册，单进程部署压根不加载那个包。
    本用例把这条结构性保证钉住 —— 哪天有人图省事在 `core/bootstrap.py` 里
    import 它，这里立刻变红。
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    hits = {name for name in _imported_full(tree) if name.startswith(MAINTENANCE)}
    assert not hits, (
        f"{module.relative_to(PROJECT_ROOT)} 导入了 {MAINTENANCE}。"
        f"租约回收只能挂在 workers/ 上：单进程模式下启用它会造成"
        f"一份任务两个执行者。"
    )


# ── 模型必须经组合根装配（PR 评审 #9）───────────────────────────────────────

#: 具体的模型实现。直接 new 它们就绕过了 `bind_gate()`，
#: 于是 `_gate is None`，闸门**静默失效**（不报错、不打日志）。
CONCRETE_MODELS = (
    "Qwen3VLEmbeddingModel",
    "OpenAIEmbeddingModel",
    "Qwen3VLReranker",
)

#: 只有这些地方可以直接构造：组合根负责装配，模型模块自己是定义处。
MAY_CONSTRUCT_MODELS = ("core/bootstrap.py",)


def _model_construction_sites(module: Path) -> set[str]:
    """找出模块里直接实例化具体模型类的地方。"""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        name = (
            called.id
            if isinstance(called, ast.Name)
            else called.attr
            if isinstance(called, ast.Attribute)
            else None
        )
        if name in CONCRETE_MODELS:
            hits.add(name)
    return hits


def _package_modules() -> list[Path]:
    root = PROJECT_ROOT / "comet_rag"
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("module", _package_modules(), ids=lambda p: p.name)
def test_models_are_only_constructed_by_the_composition_root(module: Path) -> None:
    """**并发闸门只在 `build_context()` 里挂上**（`bind_gate()`）。

    任何绕开组合根直接 new 模型的地方，`_gate` 都是 None —— `aembed` 会
    直接放行，退回"每次调用一个信号量"的老样子。而那正是本项目实测出
    "配置写 4、实际 128"的那个缺陷（见 `core/concurrency.py`）。

    危险之处在于它**不报错也不打日志**：静默地把限流关掉。
    所以用结构性守卫顶上 —— 这条与"单进程不得启用租约回收"是同一套思路：
    易错的约定，就把它变成够不着的结构。
    """
    relative = module.relative_to(PROJECT_ROOT / "comet_rag").as_posix()
    if relative.startswith("infrastructure/models/"):
        return  # 定义处自己不算
    if any(relative.endswith(allowed) for allowed in MAY_CONSTRUCT_MODELS):
        return

    hits = _model_construction_sites(module)
    assert not hits, (
        f"{module.relative_to(PROJECT_ROOT)} 直接构造了 {sorted(hits)}，"
        f"绕过了 build_context() 的 bind_gate() —— 闸门会静默失效。"
        f"请从 Context 取，或在 core/bootstrap.py 里装配。"
    )
