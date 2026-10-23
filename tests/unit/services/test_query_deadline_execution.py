from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from comet_rag.services.query_deadline import QueryDeadline, QueryDeadlineExceeded


async def test_expired_budget_does_not_create_work_and_rejects_late_success() -> None:
    deadline = QueryDeadline(0.01)
    operation = AsyncMock(return_value=1)

    async def blocks_event_loop() -> int:
        time.sleep(0.02)
        return 1

    with pytest.raises(QueryDeadlineExceeded):
        await deadline.run(blocks_event_loop, stage="dense")
    with pytest.raises(QueryDeadlineExceeded):
        await deadline.run(operation, stage="parent")
    operation.assert_not_called()


async def test_cancel_suppressing_provider_cannot_return_late_success() -> None:
    async def swallow() -> int:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return 1
        return 2

    with pytest.raises(QueryDeadlineExceeded):
        await QueryDeadline(0.01).run(swallow, stage="dense")


async def test_fatal_failure_during_deadline_cleanup_is_not_hidden() -> None:
    async def fails_on_cancel() -> int:
        try:
            await asyncio.Event().wait()
        finally:
            raise ValueError("invalid revision")

    with pytest.raises(ValueError, match="invalid revision"):
        await QueryDeadline(0.01).run(fails_on_cancel, stage="dense")
