from __future__ import annotations

import argparse
import asyncio
import sys
from importlib import import_module
from pathlib import Path

import pytest

from comet_rag.embedder.types import EmbeddingSpace


@pytest.mark.parametrize("module_name", ["persistent_text", "persistent_multimodal"])
@pytest.mark.parametrize("failure", ["client", "prepare", "dispose", "cancel"])
def test_example_closes_owned_clients_on_setup_and_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module_name: str, failure: str
) -> None:
    module = import_module(f"examples.{module_name}")
    events = []
    databases = []
    space = EmbeddingSpace("test-only", 2, "unit-test")

    async def load(*args, **kwargs):
        return argparse.Namespace(space=space)

    class Engine:
        async def dispose(self):
            events.append("engine closed")
            if failure == "dispose":
                raise OSError("dispose failed")

    class Client:
        def __init__(self, **kwargs):
            if failure == "client":
                raise RuntimeError("client failed")
            databases.append(kwargs.get("db_name"))

        async def close(self):
            events.append("client closed")

    class Docs:
        def __init__(self, **kwargs):
            pass

        async def prepare(self):
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("prepare failed")

    monkeypatch.setenv("COMET_RAG_TEST_POSTGRES_URL", "test-only")
    monkeypatch.setattr(module.AsyncCLIPEmbedder, "load", load)
    monkeypatch.setattr(module, "create_async_engine", lambda url: Engine())
    monkeypatch.setattr(module, "AsyncMilvusClient", Client)
    monkeypatch.setattr(module, "AsyncPostgresDocStore", Docs)
    monkeypatch.setattr(module, "AsyncMilvusVectorStore", lambda **kwargs: object())
    args = argparse.Namespace(
        model_dir=tmp_path,
        milvus_uri="test-only",
        milvus_db="dedicated_test",
        scope="test-scope",
        output_dir=tmp_path,
        phase="verify",
    )

    async def scenario():
        expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
        with pytest.raises(expected) as caught:
            if module_name == "persistent_text":
                await module.run(args)
            else:
                await module.run(args, image_url=None)
        if failure == "dispose":
            assert "prepare failed" in str(caught.value)
            assert any("OSError" in note for note in caught.value.__notes__)

    asyncio.run(scenario())
    assert events == (
        ["engine closed"] if failure == "client" else ["client closed", "engine closed"]
    )
    assert databases == ([] if failure == "client" else ["dedicated_test"])


@pytest.mark.parametrize("module_name", ["persistent_text", "persistent_multimodal"])
@pytest.mark.parametrize("database", [None, "dedicated_test"])
def test_example_cli_selects_milvus_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module_name: str,
    database: str | None,
) -> None:
    module = import_module(f"examples.{module_name}")
    observed = []

    async def run(args, **kwargs):
        observed.append(args.milvus_db)

    argv = [
        module_name,
        "--model-dir",
        str(tmp_path),
        "--output-dir",
        str(tmp_path),
        "--scope",
        "test-scope",
        "--phase",
        "verify",
    ]
    if database is not None:
        argv.extend(("--milvus-db", database))
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(module, "run", run)
    module.main()
    assert observed == [database or "default"]
