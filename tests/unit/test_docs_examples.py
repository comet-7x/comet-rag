"""文档防腐守卫。

`docs/pipeline_usage.md` 曾整体落后于代码一个重构：hook 签名少一个参数、
调用了并不存在的 `clean_to_string`、import 路径少一层 `models`。
照抄文档的示例代码会直接报错 —— 对开源项目来说这是第一批用户的第一印象。

本测试不执行示例（多数需要真实 docx 或远程模型服务），而是做两件足以拦住
上述全部问题的静态检查：

1. **语法**：每个 python 代码块都能被 `ast.parse` 解析
2. **符号**：块内每一个 `from comet_rag...import X` 的 X 都真实存在

第 2 条是关键 —— `clean_to_string` 和错误的 embedding 路径都会在这里当场暴露。
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = PROJECT_ROOT / "docs"

_CODE_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _iter_docs() -> list[Path]:
    """docs/*.md **加上 README** —— README 是第一批用户看到的第一段代码，
    却曾经不在守卫范围内。补进来的当天就抓到一个错的示例：
    `for chunk in pipeline.run(...)`，而 `run()` 返回的是 `PipelineResult`，
    真正可迭代的是 `result.chunks`。"""
    return [PROJECT_ROOT / "README.md", *sorted(DOCS_DIR.glob("*.md"))]


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
