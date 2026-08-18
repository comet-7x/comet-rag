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

#: `engines/` 允许依赖的本项目包。**白名单，不是黑名单。**
#:
#: 这里原本是一份黑名单（api / workers / services）。黑名单只拦得住已经想到
#: 的那几个包，于是新增的 `application/` 从缝里溜了进去 —— `pipeline.py` 运行时
#: import 了它，而 architecture.md 白纸黑字写着 engines 在最底层、依赖只能向下。
#: 文档说的规则和守卫执行的规则不是同一条，正好差出一个洞。
#:
#: 白名单没有这个失效模式：新包默认被拒，要放行必须来这里改一行、并说明理由。
ENGINES_MAY_IMPORT = ("comet_rag.engines", "comet_rag.ports")


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


def _package_parts(module: Path) -> list[str]:
    """模块所在包的点分路径，用于解析相对导入。"""
    parts = list(module.relative_to(PROJECT_ROOT).with_suffix("").parts)
    parts.pop()  # `__init__` 或模块名，两种情况下要的都是它所在的目录
    return parts


def _imported_full(tree: ast.AST, module: Path | None = None) -> set[str]:
    """收集导入的完整模块名；**相对导入会被解析成绝对名**。

    这里原本直接跳过 `level > 0`，于是 `from ...services import x` 这类相对
    写法能绕过**全部**分层守卫 —— 守卫看不见的依赖等于没有守卫。仓库里当前
    的相对导入恰好都在包内，所以没有真实违规，但那是巧合，不是保证。

    `module is None` 时无从解析相对导入（合成 AST 的自检用例会这样调），
    这种情况下仍然跳过。
    """
    names: set[str] = set()
    package = _package_parts(module) if module is not None else None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    names.add(node.module)
            elif package is not None:
                # level=1 是当前包，每多一级往上退一层
                base = package[: len(package) - (node.level - 1)]
                if base:
                    names.add(".".join([*base, node.module] if node.module else base))
    return names


@pytest.mark.parametrize("module", _iter_engine_modules(), ids=lambda p: p.name)
def test_engines_do_not_import_infrastructure(module: Path) -> None:
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    violations = _imported_roots(tree) & FORBIDDEN_IN_ENGINES
    assert not violations, (
        f"{module.relative_to(PROJECT_ROOT)} 违反 spec A1，导入了基础设施包：{sorted(violations)}。"
        f"若确需此能力，应放到 infrastructure/ 并通过接口注入。"
    )


def _engine_internal_violations(tree: ast.AST, module: Path | None = None) -> set[str]:
    return {
        name
        for name in _imported_full(tree, module)
        if name.startswith("comet_rag.") and not name.startswith(ENGINES_MAY_IMPORT)
    }


@pytest.mark.parametrize("module", _iter_engine_modules(), ids=lambda p: p.name)
def test_engines_only_depend_on_engines_and_ports(module: Path) -> None:
    """**`engines/` 是依赖图的底，只能向下看。**

    它唯一被允许依赖的本项目包是 `ports/`（纯 Protocol，零依赖）。那正是
    "库那一半"能被单独拿去用的前提：装一个 docx 解析器不该顺带拖进
    FastAPI、Milvus，或者任何一层用例编排。
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    violations = _engine_internal_violations(tree, module)
    assert not violations, (
        f"{module.relative_to(PROJECT_ROOT)} 依赖了 engines/ports 之外的本项目包："
        f"{sorted(violations)}。engines 只能依赖 {list(ENGINES_MAY_IMPORT)} —— "
        f"确需放行请修改 ENGINES_MAY_IMPORT 并在那里写明理由。"
    )


def test_guard_actually_detects_violations() -> None:
    """守卫自检：确保上面两个测试不是永远为真。

    第二条特意用 `comet_rag.application` 举例 —— 那正是旧黑名单漏掉的那个包。
    """
    tree = ast.parse(
        "import sqlalchemy\n"
        "from comet_rag.api import deps\n"
        "from comet_rag.application.embedding_batch import aembed_documents\n"
        "from comet_rag.ports import EmbeddingPort\n"
    )
    assert _imported_roots(tree) & FORBIDDEN_IN_ENGINES == {"sqlalchemy"}
    assert _engine_internal_violations(tree) == {
        "comet_rag.api",
        "comet_rag.application.embedding_batch",
    }


# ── 业务/引擎依赖模型 Port，而不是供应商适配器 ─────────────────────────────

MODEL_ADAPTER_PACKAGE = "comet_rag.infrastructure.providers"


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
        for name in _imported_full(tree, module)
        if name.startswith(MODEL_ADAPTER_PACKAGE)
    }
    assert not hits, (
        f"{module.relative_to(PROJECT_ROOT)} 直接依赖了模型适配器：{sorted(hits)}。"
        "请依赖 comet_rag.ports，并在 composition/bootstrap.py 装配实现。"
    )


# ── 单进程模式不得挂上租约回收（T24）────────────────────────────────────────

#: 除了 workers/ 自己，谁都不该 import 它。写成路径前缀，子模块一并覆盖。
MAINTENANCE = "comet_rag.workers.maintenance"

#: 单进程部署会加载的东西：组合根、API、以及任务框架本身
SINGLE_PROCESS_TREES = (
    "core",
    "composition",
    "api",
    "tasks",
    "services",
    "engines",
    "infrastructure",
)


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
    本用例把这条结构性保证钉住 —— 哪天有人图省事在 `composition/bootstrap.py` 里
    import 它，这里立刻变红。
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    hits = {name for name in _imported_full(tree, module) if name.startswith(MAINTENANCE)}
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
MAY_CONSTRUCT_MODELS = ("composition/bootstrap.py",)


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
    if relative.startswith("infrastructure/providers/"):
        return  # 定义处自己不算
    if any(relative.endswith(allowed) for allowed in MAY_CONSTRUCT_MODELS):
        return

    hits = _model_construction_sites(module)
    assert not hits, (
        f"{module.relative_to(PROJECT_ROOT)} 直接构造了 {sorted(hits)}，"
        f"绕过了 build_context() 的 bind_gate() —— 闸门会静默失效。"
        f"请从 Context 取，或在 core/bootstrap.py 里装配。"
    )


# ── `core/` 是零依赖内核（第 4 条守卫）────────────────────────────────────


def _core_modules() -> list[Path]:
    return sorted(
        p
        for p in (PROJECT_ROOT / "comet_rag" / "core").rglob("*.py")
        if "__pycache__" not in p.parts
    )


@pytest.mark.parametrize("module", _core_modules(), ids=lambda p: p.name)
def test_core_is_a_zero_dependency_kernel(module: Path) -> None:
    """**`core/` 不得 import 本项目任何其他包。**

    这里装的是日志、追踪、并发闸门、降级控制器 —— 被 `services/`、
    `infrastructure/`、`tasks/`、`api/` 全都依赖的横切设施。既然人人依赖它，
    它就必须在依赖图的最底下，否则立刻出环。

    这条规则此前写不出来，因为 `core/` 同时还装着组合根（`bootstrap.py`、
    `context.py`），而组合根依赖所有人。同一个包里两个方向相反的东西，
    依赖图上 `core` 的箭头就自相矛盾：既指向 services，又被 services 指向。
    组合根已迁至 `composition/`，这条守卫才成立。
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    violations = {
        name
        for name in _imported_full(tree, module)
        if name.startswith("comet_rag.") and not name.startswith("comet_rag.core")
    }
    assert not violations, (
        f"{module.relative_to(PROJECT_ROOT)} 依赖了 {sorted(violations)}。"
        f"core/ 是零依赖内核，人人依赖它；它一旦回头依赖上层就会出环。"
        f"需要上层能力的东西属于 composition/ 或 services/。"
    )


def test_core_guard_actually_detects_violations() -> None:
    """守卫自检：确保上面那条不是永远为真。"""
    tree = ast.parse(
        "from comet_rag.core.logging import logger\n"
        "from comet_rag.services.retrieval import RetrievalService\n"
    )
    violations = {
        name
        for name in _imported_full(tree)
        if name.startswith("comet_rag.") and not name.startswith("comet_rag.core")
    }
    assert violations == {"comet_rag.services.retrieval"}


# ── 包级依赖不得成环（第 5 条守卫）──────────────────────────────────────────


def _package_edges() -> dict[str, set[str]]:
    """把模块级 import 收敛成顶层包之间的有向边。"""
    root = PROJECT_ROOT / "comet_rag"
    edges: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root)
        source = relative.parts[0] if len(relative.parts) > 1 else "(top)"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _imported_full(tree, path):
            if not name.startswith("comet_rag."):
                continue
            target = name.split(".")[1]
            if target != source:
                edges.setdefault(source, set()).add(target)
    return edges


def _find_cycle(edges: dict[str, set[str]]) -> list[str] | None:
    """返回任意一条环（含首尾同名），没有环时返回 None。"""
    state: dict[str, int] = {}
    stack: list[str] = []

    def walk(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            if state.get(nxt) == 1:
                return stack[stack.index(nxt) :] + [nxt]
            if state.get(nxt) is None and (found := walk(nxt)):
                return found
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(edges):
        if state.get(node) is None and (found := walk(node)):
            return found
    return None


def test_no_package_level_import_cycles() -> None:
    """**顶层包之间不得出现循环依赖。**

    此前 `infrastructure` 与 `tasks` 互指：`knowledge_base.py` 只为取个当前
    时间就 import 了 `tasks.models.Time`，而 `store_postgres.py` 反过来 import
    `infrastructure.database`。

    环的代价不在于 Python 跑不起来（它跑得起来），而在于**这两个包再也不能
    单独理解或单独拿走**：读任一个都得先读另一个，而且谁先初始化取决于导入
    顺序。`Time` 是个只依赖标准库的时间工具，跟"任务"毫无关系，挪进
    `core/` 环就断了。

    逐条列出所有边太脆（每加一个 import 就要改测试），所以这里只断言**无环**
    —— 这是结构性质，不是清单。
    """
    cycle = _find_cycle(_package_edges())
    assert cycle is None, (
        f"顶层包出现循环依赖：{' → '.join(cycle)}。"
        f"环里的包无法被单独理解或单独复用；请把被共用的那部分下沉到"
        f"两者都能依赖的低层包（如 core/ 或 ports/）。"
    )


def test_cycle_detector_actually_finds_cycles() -> None:
    """守卫自检：环检测本身要能真的发现环。"""
    assert _find_cycle({"a": {"b"}, "b": {"c"}, "c": {"a"}}) is not None
    assert _find_cycle({"a": {"b"}, "b": {"c"}}) is None


# ── 相对导入必须被解析（守卫自身的盲区）────────────────────────────────────


def test_relative_imports_are_resolved_to_absolute_names() -> None:
    """**相对导入不解析，等于所有分层守卫都有一个后门。**

    `from ...services import x` 与 `from comet_rag.services import x` 是同一
    件事，但 AST 里前者的 `module` 只是 `"services"`、`level=3`。早先的
    `_imported_full` 直接跳过 `level > 0`，于是相对写法能绕过全部五条守卫。

    仓库当前的相对导入恰好都在包内，所以没有真实违规 —— 但那是巧合。
    这条用例把解析本身钉死。
    """
    module = PROJECT_ROOT / "comet_rag" / "engines" / "pipelines" / "pipeline.py"
    tree = ast.parse(
        "from .types import Chunk\n"        # level=1 → 同包
        "from ..loaders import Auto\n"      # level=2 → comet_rag.engines.loaders
        "from ...services import Foo\n"     # level=3 → comet_rag.services（违规）
        "from ...ports import EmbeddingPort\n"
    )

    assert _imported_full(tree, module) == {
        "comet_rag.engines.pipelines.types",
        "comet_rag.engines.loaders",
        "comet_rag.services",
        "comet_rag.ports",
    }
    # 解析之后，越层的那条才拦得住
    assert _engine_internal_violations(tree, module) == {"comet_rag.services"}


def test_package_parts_handles_both_module_and_package_init() -> None:
    """`__init__.py` 的"所在包"是它自己的目录，普通模块是它的父目录。"""
    root = PROJECT_ROOT / "comet_rag"
    assert _package_parts(root / "engines" / "pipelines" / "pipeline.py") == [
        "comet_rag",
        "engines",
        "pipelines",
    ]
    assert _package_parts(root / "engines" / "chunkers" / "__init__.py") == [
        "comet_rag",
        "engines",
        "chunkers",
    ]


def test_cycle_detector_sees_relative_imports() -> None:
    """环检测同样不能被相对导入绕过。"""
    module = PROJECT_ROOT / "comet_rag" / "infrastructure" / "knowledge_base.py"
    tree = ast.parse("from ..tasks.models import Time\n")
    assert _imported_full(tree, module) == {"comet_rag.tasks.models"}
