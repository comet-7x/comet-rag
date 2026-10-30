from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

PACKAGE = "comet_rag"
ROOT = Path(__file__).resolve().parents[2] / PACKAGE
SDK_MODULES = {
    "anthropic",
    "openai",
    "asyncpg",
    "boto3",
    "botocore",
    "httpx",
    "PIL",
    "pymilvus",
    "sqlalchemy",
    "torch",
    "transformers",
}
ADAPTERS = {
    "sources.http",
    "sources.s3",
    "docstores.postgres",
    "vectorstores.milvus",
    "documents.parsers.image",
    "documents.parsers.mineru",
}


def _imports(code: str, module: str, *, package: bool = False) -> tuple[str, ...]:
    names = []
    parent = module if package else module.rpartition(".")[0]
    tree = ast.parse(code)
    type_only = set()
    if module == f"{PACKAGE}.errors":
        for statement in tree.body:
            if not isinstance(statement, ast.If):
                continue
            if not isinstance(statement.test, ast.Name):
                continue
            if statement.test.id != "TYPE_CHECKING":
                continue
            for node in statement.body:
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.level or node.module != f"{PACKAGE}.pipelines.types":
                    continue
                if [alias.name for alias in node.names] == ["IngestionResult"]:
                    type_only.add(node)
    for node in ast.walk(tree):
        if node in type_only:
            continue
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = "." * node.level + (node.module or "")
            base = resolve_name(name, parent) if node.level else name
            if node.module is not None:
                names.append(base)
            names.extend(
                f"{base}.{alias.name}" for alias in node.names if alias.name != "*"
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.append(node.args[0].value)
    return tuple(names)


def _violations(module: str, imports: tuple[str, ...]) -> list[str]:
    local = module.removeprefix(PACKAGE + ".")
    pure_allowed = {
        "content": set(),
        "identity": set(),
        "errors": set(),
        "_urls": set(),
        "documents.schemas": {
            f"{PACKAGE}._urls",
            f"{PACKAGE}.content",
            f"{PACKAGE}.identity",
            f"{PACKAGE}.documents.codec",
        },
    }
    violations = []
    for target in imports:
        if local in pure_allowed and (
            target.startswith(PACKAGE + ".")
            and target not in pure_allowed[local]
            and target.rpartition(".")[0] not in pure_allowed[local]
            or target.split(".")[0] in SDK_MODULES
        ):
            violations.append(target)
        if local.startswith(("embedder.", "rerankers.")) and target.startswith(
            f"{PACKAGE}.indexing"
        ):
            violations.append(target)
        if local.startswith("indexing.") and target.startswith(
            (f"{PACKAGE}.docstores", f"{PACKAGE}.vectorstores")
        ):
            violations.append(target)
        if (
            target.split(".")[0] in SDK_MODULES
            and local not in ADAPTERS
            and not local.startswith(("embedder.models.", "rerankers.models."))
        ):
            violations.append(target)
    return violations


def test_components_preserve_import_boundaries() -> None:
    errors = []
    for path in ROOT.rglob("*.py"):
        parts = path.relative_to(ROOT.parent).with_suffix("").parts
        module = ".".join(parts[:-1] if path.name == "__init__.py" else parts)
        imports = _imports(path.read_text(), module, package=path.name == "__init__.py")
        errors.extend(
            f"{module} -> {target}" for target in _violations(module, imports)
        )
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize(
    ("module", "code"),
    [
        (f"{PACKAGE}.content", "import httpx"),
        (f"{PACKAGE}.errors", "import httpx"),
        (f"{PACKAGE}.errors", f"from {PACKAGE}.sources import SourceError"),
        (f"{PACKAGE}.errors", f"from {PACKAGE}.pipelines.types import IngestionResult"),
        (
            f"{PACKAGE}.errors",
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import httpx",
        ),
        (f"{PACKAGE}.content", "import openai"),
        (f"{PACKAGE}.content", "import anthropic"),
        (f"{PACKAGE}.content", f"from {PACKAGE} import indexing"),
        (f"{PACKAGE}.content", "from . import indexing"),
        (f"{PACKAGE}.embedder.models.bad", f"from {PACKAGE} import indexing"),
        (f"{PACKAGE}.indexing.builders.bad", "from ... import docstores"),
        (
            f"{PACKAGE}.documents.schemas",
            f"from {PACKAGE}.pipelines import AsyncSearchPipeline",
        ),
        (f"{PACKAGE}.embedder.models.bad", "from ...indexing.types import IndexUnit"),
        (f"{PACKAGE}.indexing.builders.bad", "from ...docstores import AsyncDocStore"),
        (f"{PACKAGE}.sources.base", 'import_module("boto3")'),
    ],
)
def test_import_guard_rejects_corresponding_defects(module: str, code: str) -> None:
    assert _violations(module, _imports(code, module))


@pytest.mark.parametrize(
    ("module", "code", "package"),
    [
        (f"{PACKAGE}.documents.schemas", "from ...content import MediaRef", True),
        (f"{PACKAGE}.documents.schemas", "from .. import codec", True),
        (
            f"{PACKAGE}.errors",
            f"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from {PACKAGE}.pipelines.types import IngestionResult",
            False,
        ),
        (
            f"{PACKAGE}.embedder.models.clip",
            f"from {PACKAGE}.content import TextPart",
            False,
        ),
    ],
)
def test_import_guard_accepts_permitted_imports(
    module: str, code: str, package: bool
) -> None:
    assert not _violations(module, _imports(code, module, package=package))


def test_removed_runtime_paths_are_absent() -> None:
    old = (
        "api",
        "workers",
        "tasks",
        "composition",
        "services",
        "infrastructure",
        "engines",
        "persistence",
        "ports",
        "loaders",
        "legacy",
        "cli.py",
        "pipeline.py",
    )
    assert not [name for name in old if (ROOT / name).exists()]
