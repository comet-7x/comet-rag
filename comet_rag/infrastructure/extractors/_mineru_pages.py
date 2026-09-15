from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TypeGuard

from comet_rag.ports import ExtractedPage

_IGNORED_PAGE_AUXILIARY_TYPES = frozenset(
    {"header", "footer", "page_number", "aside_text", "page_footnote"}
)


def extract_mineru_pages(content_list: object, /) -> tuple[ExtractedPage, ...] | None:
    """从 legacy content_list 重建页 Markdown；不能无损重建就拒绝页事实。"""
    if isinstance(content_list, str):
        try:
            content_list = json.loads(content_list)
        except ValueError:
            return None
    if not isinstance(content_list, list) or not content_list:
        return None

    pages: list[ExtractedPage] = []
    current_page_index: int | None = None
    current_blocks: list[str] = []

    for item in content_list:
        if not isinstance(item, Mapping):
            return None
        page_index = item.get("page_idx")
        if not _is_int(page_index) or page_index < 0:
            return None
        if current_page_index is not None and page_index < current_page_index:
            return None
        if current_page_index != page_index:
            if current_page_index is not None and current_blocks:
                pages.append(_page(current_page_index, current_blocks))
            current_page_index = page_index
            current_blocks = []

        rendered = _render_item(item)
        if rendered is None:
            return None
        if rendered:
            current_blocks.append(rendered)

    if current_page_index is not None and current_blocks:
        pages.append(_page(current_page_index, current_blocks))
    return tuple(pages) or None


def _page(page_index: int, blocks: list[str]) -> ExtractedPage:
    return ExtractedPage(
        page_number=page_index + 1,
        markdown="\n\n".join(blocks),
    )


def _render_item(item: Mapping[object, object]) -> str | None:
    item_type = item.get("type")
    if item_type in _IGNORED_PAGE_AUXILIARY_TYPES:
        return ""
    if item_type == "text":
        text = item.get("text")
        if not isinstance(text, str):
            return None
        level = item.get("text_level")
        if level is None:
            return text.strip()
        if not _is_int(level) or not 1 <= level <= 6:
            return None
        return f"{'#' * level} {text.strip()}" if text.strip() else ""
    if item_type == "equation":
        text = item.get("text")
        return text.strip() if isinstance(text, str) else None
    if item_type == "code":
        return _render_code(item)
    # image/table/chart/list 会丢失原 block 顺序或渲染信息，不能据此声明精确页边界。
    return None


def _render_code(item: Mapping[object, object]) -> str | None:
    body = item.get("code_body")
    captions = item.get("code_caption", [])
    footnotes = item.get("code_footnote", [])
    if not isinstance(body, str):
        return None
    if not _is_string_list(captions) or not _is_string_list(footnotes):
        return None
    parts = [*captions, body, *footnotes]
    return "  \n".join(part.strip() for part in parts if part.strip())


def _is_string_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = ["extract_mineru_pages"]
