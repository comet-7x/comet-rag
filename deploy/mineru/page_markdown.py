from __future__ import annotations

import os
from collections.abc import Callable
from copy import deepcopy

EXTENSION = "comet_page_markdown"
ENVIRONMENT = "COMET_MINERU_PAGE_MARKDOWN_V1"


def enabled() -> bool:
    """只在独立 MinerU 部署中启用；配置错误不能静默变为旧行为。"""
    value = os.environ.get(ENVIRONMENT, "0")
    if value == "0":
        return False
    if value != "1":
        raise ValueError(f"{ENVIRONMENT} 必须为 0 或 1")
    if os.environ.get("MINERU_TABLE_MERGE_ENABLE", "").lower() != "false":
        raise ValueError("逐页输出要求显式设置 MINERU_TABLE_MERGE_ENABLE=false")
    return True


def _reject_moved_content(value: object) -> None:
    if isinstance(value, dict):
        if value.get("cross_page") or (
            value.get("type") == "table_body" and value.get("lines_deleted")
        ):
            raise ValueError("发现跨页移动内容，不能声明物理页事实")
        for item in value.values():
            _reject_moved_content(item)
    elif isinstance(value, list):
        for item in value:
            _reject_moved_content(item)


def render_pages(
    pdf_info: list[dict[str, object]],
    render: Callable[[list[dict[str, object]], str, str], str],
    mode: str,
    image_dir: str,
    markdown: str,
) -> dict[str, object]:
    """复用调用方的上游渲染器；这里只处理页身份、封装和完整性。"""
    if not enabled():
        raise ValueError("逐页部署扩展未启用")
    if not pdf_info:
        raise ValueError("逐页输出不能使用空 PDF 清单")
    pages = []
    fragments = []
    for index, page in enumerate(pdf_info):
        if type(page.get("page_idx")) is not int or page["page_idx"] != index:
            raise ValueError("只支持完整 PDF，物理页必须从 0 连续递增")
        _reject_moved_content(page)
        # 上游渲染可能规范化内部 span；副本避免第二次渲染改变后续产物。
        fragment = render([deepcopy(page)], mode, image_dir)
        if not isinstance(fragment, str):
            raise ValueError("上游逐页渲染必须返回 Markdown 字符串")
        pages.append({"page_idx": index, "markdown": fragment})
        if fragment:
            fragments.append(fragment)
    if "\n\n".join(fragments) != markdown:
        raise ValueError("逐页 Markdown 未精确重建原生全文")
    return {
        "version": 1,
        "boundary_mode": "physical",
        "page_count": len(pages),
        "pages": pages,
    }
