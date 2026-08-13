"""每个模块都必须可导入。

**为什么需要这条**：T5 删掉 `comet_rag/schemas/task.py` 时漏了
`schemas/__init__.py` 里的 `from .task import ...`，`import comet_rag.schemas`
从那时起一直是坏的 —— 没有任何测试碰它，600 多个用例全绿。

当时的验证命令是
`grep -rn "..." comet_rag/ --include=*.py || echo "✅ 无残留"`，
zsh 对 `--include=*.py` 做 glob 展开失败导致 grep **从未执行**，
`||` 分支却打印了"无残留"。一条空转的检查比没有检查更糟：它给出虚假的安全感。

这个测试用结构性的方式取代人眼：遍历包内每个模块并真的 import 一遍。
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import comet_rag

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 需要 optional extras 的模块。缺少对应依赖时跳过而非失败 ——
#: 核心依赖环境下它们本来就不该可导入（spec A1）。
OPTIONAL = {
    "comet_rag.infrastructure.vectorstore.milvus": "pymilvus",
    "comet_rag.tasks.store_postgres": "sqlalchemy",
    "comet_rag.tasks.executor_arq": "arq",
}


def _all_modules() -> list[str]:
    names = []
    for info in pkgutil.walk_packages(
        comet_rag.__path__, prefix="comet_rag.", onerror=lambda _: None
    ):
        names.append(info.name)
    return sorted(names)


MODULES = _all_modules()


def test_package_has_modules() -> None:
    """若遍历逻辑坏掉导致零模块，下面的参数化会静默变成零用例。"""
    assert len(MODULES) > 20, f"只发现 {len(MODULES)} 个模块，遍历逻辑可能坏了"


@pytest.mark.parametrize("module_name", MODULES)
def test_module_is_importable(module_name: str) -> None:
    required = OPTIONAL.get(module_name)
    if required is not None:
        pytest.importorskip(required, reason=f"{module_name} 需要 {required}")

    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"{module_name} 无法导入：{exc}。"
            f"若它依赖 optional extra，请加进本文件的 OPTIONAL 表。"
        )


def test_importing_api_does_not_require_a_config_file() -> None:
    """`import comet_rag.api.main` 不得读配置。

    否则文档守卫、静态检查、任何只想 import 一下的工具，
    都会被一份缺字段的 config.yaml 拦住。
    """
    module = importlib.import_module("comet_rag.api.main")

    assert hasattr(module, "create_app")
