from __future__ import annotations

import asyncio
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "CometRAGError",
        "SourceError",
        "SourceResourceLimitError",
        "ArchiveResourceLimitError",
        "IngestionIncompleteError",
    ],
)
def test_custom_errors_are_defined_in_central_module(name: str) -> None:
    from comet_rag import errors

    error = getattr(errors, name)
    assert error.__module__ == "comet_rag.errors"


def test_component_errors_share_library_base_and_builtin_categories() -> None:
    from comet_rag import CometRAGError
    from comet_rag.errors import (
        ArchiveResourceLimitError,
        IngestionIncompleteError,
        SourceError,
        SourceResourceLimitError,
    )
    from comet_rag.errors import CometRAGError as DefinedError

    assert CometRAGError is DefinedError
    for error in (
        SourceError,
        SourceResourceLimitError,
        ArchiveResourceLimitError,
    ):
        assert issubclass(error, CometRAGError)
        assert issubclass(error, ValueError)
        with pytest.raises(CometRAGError):
            raise error("failure")
    assert issubclass(SourceResourceLimitError, SourceError)
    assert issubclass(IngestionIncompleteError, CometRAGError)
    assert issubclass(IngestionIncompleteError, RuntimeError)
    assert not issubclass(ValueError, CometRAGError)
    assert not issubclass(asyncio.CancelledError, CometRAGError)


def test_importing_errors_does_not_load_components_or_sdks(tmp_path: Path) -> None:
    code = """
import sys
from comet_rag.errors import (
    ArchiveResourceLimitError,
    CometRAGError,
    IngestionIncompleteError,
    SourceError,
    SourceResourceLimitError,
)

loaded = sorted(name for name in sys.modules if name.startswith("comet_rag."))
assert loaded == ["comet_rag.errors"], loaded
sdks = {"httpx", "boto3", "lxml", "PIL", "asyncpg", "sqlalchemy", "pymilvus", "torch", "transformers"}
assert not sdks.intersection(sys.modules)
"""
    subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-I", "-c", code], cwd=tmp_path, check=True
    )
