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
