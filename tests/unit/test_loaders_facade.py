from __future__ import annotations

import subprocess
import sys

import pytest

from comet_rag import loaders
from comet_rag.infrastructure.sources import (
    AutoLoader,
    LoaderContent,
    LocalLoader,
    URLLoader,
)
from comet_rag.infrastructure.sources.s3 import S3Loader
from comet_rag.ports import LoadedResource, SourceLoaderPort


def test_facade_exposes_one_discoverable_loader_entrypoint() -> None:
    assert loaders.AutoLoader is AutoLoader
    assert loaders.LocalLoader is LocalLoader
    assert loaders.URLLoader is URLLoader
    assert loaders.S3Loader is S3Loader
    assert loaders.LoaderContent is LoaderContent is LoadedResource
    assert loaders.SourceLoaderPort is SourceLoaderPort


def test_core_facade_import_does_not_load_s3_adapter() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import comet_rag.loaders; "
                "assert 'comet_rag.infrastructure.sources.s3' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_facade_dir_includes_lazy_s3_names() -> None:
    assert {"AutoLoader", "LocalLoader", "URLLoader", "S3Loader"} <= set(dir(loaders))


def test_s3_missing_dependency_error_is_actionable(monkeypatch) -> None:
    loader = S3Loader()
    monkeypatch.setitem(sys.modules, "boto3", None)

    with pytest.raises(ModuleNotFoundError, match=r"comet-rag\[server\]"):
        loader._new_sync_client()
