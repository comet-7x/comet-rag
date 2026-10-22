from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from comet_rag.api import lifespan as api_lifespan
from comet_rag.config.schemas import (
    APPConfig,
    EmbeddingModelConfig,
    InfrastructureConfig,
    ServerConfig,
)
from comet_rag.workers import base as workers
from comet_rag.workers.preprocessor import PROFILE


@pytest.fixture
def config() -> APPConfig:
    return APPConfig(
        server_config=ServerConfig(host="127.0.0.1", port=8000),
        infrastructure_config=InfrastructureConfig(
            embedding_model=EmbeddingModelConfig(
                base_url="http://unused", model_name="fake", dim=3
            )
        ),
    )


@pytest.fixture(autouse=True)
def no_log_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_lifespan, "setup_logging", lambda **kwargs: None)
    monkeypatch.setattr(workers, "setup_logging", lambda **kwargs: None)


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_api_failed_build_does_not_publish_context(
    config: APPConfig, monkeypatch: pytest.MonkeyPatch, error_type
) -> None:
    app = FastAPI()
    error = error_type("startup failed")
    builder = AsyncMock(side_effect=error)
    monkeypatch.setattr(api_lifespan, "build_context", builder)

    with pytest.raises(error_type) as caught:
        async with api_lifespan.make_lifespan(config)(app):
            pytest.fail("failed startup must not enter the serving phase")

    assert caught.value is error
    assert not hasattr(app.state, "ctx")
    builder.assert_awaited_once_with(config)


async def test_api_removes_context_before_closing_after_serving_failure(
    config: APPConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()

    async def close() -> None:
        assert not hasattr(app.state, "ctx")

    context = SimpleNamespace(close=AsyncMock(side_effect=close))

    async def build(*args, **kwargs):
        assert not hasattr(app.state, "ctx")
        return context

    monkeypatch.setattr(api_lifespan, "build_context", build)
    with pytest.raises(RuntimeError, match="serving failed"):
        async with api_lifespan.make_lifespan(config)(app):
            assert app.state.ctx is context
            raise RuntimeError("serving failed")

    assert not hasattr(app.state, "ctx")
    context.close.assert_awaited_once()


async def test_api_old_lifespan_does_not_remove_replacement_context(
    config: APPConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    context = SimpleNamespace(close=AsyncMock())
    replacement = object()
    monkeypatch.setattr(api_lifespan, "build_context", AsyncMock(return_value=context))

    async with api_lifespan.make_lifespan(config)(app):
        app.state.ctx = replacement

    assert app.state.ctx is replacement
    context.close.assert_awaited_once()


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_worker_failed_build_does_not_publish_resources(
    config: APPConfig, monkeypatch: pytest.MonkeyPatch, error_type
) -> None:
    injected_model = object()
    ctx = {
        "profile": PROFILE,
        "config": config,
        "build_kwargs": {"embedding_model": injected_model},
    }
    error = error_type("startup failed")
    builder = AsyncMock(side_effect=error)
    monkeypatch.setattr(workers, "build_context", builder)

    with pytest.raises(error_type) as caught:
        await workers.on_startup(ctx)

    assert caught.value is error
    assert "context" not in ctx
    assert "executor" not in ctx
    builder.assert_awaited_once_with(
        config, executor_lane=PROFILE.lane, embedding_model=injected_model
    )
    await workers.on_shutdown(ctx)


async def test_worker_removes_resources_before_close_and_shutdown_is_repeatable(
    config: APPConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = {"profile": PROFILE, "config": config}

    async def close() -> None:
        assert "context" not in ctx
        assert "executor" not in ctx

    context = SimpleNamespace(
        task_executor=object(), close=AsyncMock(side_effect=close)
    )

    async def build(*args, **kwargs):
        assert "context" not in ctx
        assert "executor" not in ctx
        return context

    monkeypatch.setattr(workers, "build_context", build)
    await workers.on_startup(ctx)
    assert ctx["context"] is context
    assert ctx["executor"] is context.task_executor

    await workers.on_shutdown(ctx)
    await workers.on_shutdown(ctx)
    context.close.assert_awaited_once()


async def test_worker_shutdown_does_not_remove_replacement_executor() -> None:
    context = SimpleNamespace(task_executor=object(), close=AsyncMock())
    replacement = object()
    ctx = {"profile": PROFILE, "context": context, "executor": replacement}

    await workers.on_shutdown(ctx)

    assert "context" not in ctx
    assert ctx["executor"] is replacement
    context.close.assert_awaited_once()
