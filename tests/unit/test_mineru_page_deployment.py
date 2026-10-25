from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from deploy.mineru.page_markdown import ENVIRONMENT, enabled, render_pages


@pytest.fixture(autouse=True)
def physical_mode(monkeypatch):
    monkeypatch.setenv(ENVIRONMENT, "1")
    monkeypatch.setenv("MINERU_TABLE_MERGE_ENABLE", "false")


def render(pages: list[dict[str, object]], mode: str, image_dir: str) -> str:
    assert mode == "mm_markdown" and image_dir == "images"
    return str(pages[0]["rendered"])


def test_deployment_reuses_renderer_and_preserves_empty_physical_pages() -> None:
    pages: list[dict[str, object]] = [
        {"page_idx": 0, "rendered": "<table>A</table>"},
        {"page_idx": 1, "rendered": ""},
        {"page_idx": 2, "rendered": "![](images/b.jpg)\n\n- C"},
    ]
    expected = deepcopy(pages)

    def mutating_renderer(items, mode, image_dir):
        result = render(items, mode, image_dir)
        items[0]["page_idx"] = 99
        return result

    result = render_pages(
        pages,
        mutating_renderer,
        "mm_markdown",
        "images",
        "<table>A</table>\n\n![](images/b.jpg)\n\n- C",
    )
    assert pages == expected
    assert result == {
        "version": 1,
        "boundary_mode": "physical",
        "page_count": 3,
        "pages": [
            {"page_idx": 0, "markdown": "<table>A</table>"},
            {"page_idx": 1, "markdown": ""},
            {"page_idx": 2, "markdown": "![](images/b.jpg)\n\n- C"},
        ],
    }


@pytest.mark.parametrize("merge", [None, "true", "0", "typo"])
def test_cross_page_merge_must_be_explicitly_disabled(monkeypatch, merge) -> None:
    if merge is None:
        monkeypatch.delenv("MINERU_TABLE_MERGE_ENABLE")
    else:
        monkeypatch.setenv("MINERU_TABLE_MERGE_ENABLE", merge)
    with pytest.raises(ValueError, match="MINERU_TABLE_MERGE_ENABLE"):
        enabled()


def test_extension_is_explicitly_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv(ENVIRONMENT)
    assert enabled() is False
    with pytest.raises(ValueError, match="未启用"):
        render_pages([], render, "mm_markdown", "images", "")
    monkeypatch.setenv(ENVIRONMENT, "true")
    with pytest.raises(ValueError, match="0 或 1"):
        enabled()


@pytest.mark.parametrize(
    "fault",
    ["empty", "bool", "gap", "moved", "merged_table", "mismatch", "render_type"],
)
def test_native_page_producer_rejects_false_page_facts(fault: str) -> None:
    pages: list[dict[str, object]] = [{"page_idx": 0, "rendered": "A"}]
    renderer = render
    if fault == "empty":
        pages = []
    elif fault == "bool":
        pages[0]["page_idx"] = False
    elif fault == "gap":
        pages[0]["page_idx"] = 1
    elif fault == "moved":
        pages[0]["para_blocks"] = [{"lines": [{"spans": [{"cross_page": True}]}]}]
    elif fault == "merged_table":
        pages[0]["para_blocks"] = [
            {"blocks": [{"type": "table_body", "lines_deleted": True}]}
        ]
    elif fault == "mismatch":
        pages[0]["rendered"] = "Wrong"
    else:

        def invalid_renderer(*_) -> str:
            return None  # type: ignore[return-value]  # 故障注入：上游违反渲染返回值契约。

        renderer = invalid_renderer
    with pytest.raises(ValueError):
        render_pages(pages, renderer, "mm_markdown", "images", "A")


def test_same_page_text_merges_are_allowed() -> None:
    pages: list[dict[str, object]] = [
        {
            "page_idx": 0,
            "rendered": "A",
            "para_blocks": [{"type": "text", "lines_deleted": True}],
        }
    ]
    assert render_pages(pages, render, "mm_markdown", "images", "A")["page_count"] == 1


@pytest.mark.parametrize("fault", ["version", "hash", "already_installed"])
def test_installer_refuses_incompatible_sources_before_writes(
    tmp_path: Path, monkeypatch, fault: str
) -> None:
    import hashlib
    import json

    from deploy.mineru import install

    source = tmp_path / "mineru/cli/common.py"
    source.parent.mkdir(parents=True)
    source.write_text("# frozen\n")
    lock = {
        "version": "3.4.5",
        "files": {
            "mineru/cli/common.py": hashlib.sha256(source.read_bytes()).hexdigest()
        },
    }
    (tmp_path / "upstream.json").write_text(json.dumps(lock))
    monkeypatch.setattr(install, "DATA", tmp_path)
    if fault == "hash":
        source.write_text("# changed\n")
    elif fault == "already_installed":
        (source.parent / "comet_page_markdown.py").write_text("# existing\n")
    before = source.read_bytes()
    with pytest.raises(ValueError):
        install.install(tmp_path, "4.0.0" if fault == "version" else "3.4.5")
    assert source.read_bytes() == before


def test_installer_rolls_back_if_a_target_write_fails(
    tmp_path: Path, monkeypatch
) -> None:
    import hashlib
    import json

    from deploy.mineru import install

    resources = tmp_path / "resources"
    resources.mkdir()
    root = tmp_path / "site"
    originals = {"mineru/cli/a.py": b"x = 1\n", "mineru/cli/b.py": b"y = 2\n"}
    for name, raw in originals.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (resources / "upstream.json").write_text(
        json.dumps(
            {
                "version": "3.4.5",
                "files": {
                    name: hashlib.sha256(raw).hexdigest()
                    for name, raw in originals.items()
                },
            }
        )
    )
    (resources / "page_markdown.py").write_text("# helper\n")
    (resources / "mineru-3.4.5-page-markdown.patch").write_text("fixture")
    monkeypatch.setattr(install, "DATA", resources)

    def apply_patch(command, **kwargs):
        staging = Path(command[-1])
        for name in originals:
            (staging / name).write_text("z = 3\n")

    monkeypatch.setattr(install.subprocess, "run", apply_patch)
    write_bytes = Path.write_bytes
    injected = False

    def failing_write(path: Path, raw: bytes) -> int:
        nonlocal injected
        if path == root / "mineru/cli/b.py" and not injected:
            injected = True
            raise OSError("Injected write failure")
        return write_bytes(path, raw)

    monkeypatch.setattr(Path, "write_bytes", failing_write)
    with pytest.raises(OSError, match="Injected"):
        install.install(root, "3.4.5")
    assert injected
    assert {name: (root / name).read_bytes() for name in originals} == originals
    assert not (root / "mineru/cli/comet_page_markdown.py").exists()
