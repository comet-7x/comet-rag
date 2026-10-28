from __future__ import annotations

from collections.abc import Sequence

from comet_rag.content import text_view
from comet_rag.docstores.base import AsyncDocStore
from comet_rag.indexing.types import IndexUnit


async def select_contexts(
    units: Sequence[IndexUnit],
    *,
    docstore: AsyncDocStore,
    expand_parents: bool,
    budget: int | None,
) -> tuple[tuple[IndexUnit, ...], tuple[str, ...]]:
    """扩展一层显式父内容；字符预算只选择完整单元，不截断原内容或资产。"""
    parent_ids = tuple(
        dict.fromkeys(
            unit.parent_unit_id
            for unit in units
            if expand_parents and unit.parent_unit_id is not None
        )
    )
    parents = await docstore.get_units(parent_ids) if parent_ids else {}
    if set(parents) != set(parent_ids):
        raise ValueError("required parent context is missing")
    if any(parent.unit_id != parent_id for parent_id, parent in parents.items()):
        raise ValueError("parent context identity does not match requested ID")
    selected: list[IndexUnit] = []
    seen: set[str] = set()
    used = omitted = 0
    for unit in units:
        context = (
            parents[unit.parent_unit_id]
            if expand_parents and unit.parent_unit_id
            else unit
        )
        if (
            context.index_scope,
            context.index_profile_id,
            context.source_id,
            context.document_id,
        ) != (
            unit.index_scope,
            unit.index_profile_id,
            unit.source_id,
            unit.document_id,
        ):
            raise ValueError(
                "parent context belongs to a different scope/profile/snapshot"
            )
        if context.unit_id in seen:
            continue
        seen.add(context.unit_id)
        size = len(text_view(context.content))
        if budget is not None and used + size > budget:
            omitted += 1
            continue
        used += size
        selected.append(context)
    diagnostics = (f"context_budget_omitted:{omitted}",) if omitted else ()
    return tuple(selected), diagnostics
