from __future__ import annotations

import asyncio

import pytest

from comet_rag.documents.parsers.archive import ArchiveResourceLimitExceeded
from comet_rag.pipelines import IngestionIncomplete
from comet_rag.sources import SourceError, SourceResourceLimitExceeded


def test_component_errors_share_library_base_and_builtin_categories() -> None:
    from comet_rag import CometRAGError
    from comet_rag.errors import CometRAGError as DefinedError

    assert CometRAGError is DefinedError
    for error in (
        SourceError,
        SourceResourceLimitExceeded,
        ArchiveResourceLimitExceeded,
    ):
        assert issubclass(error, CometRAGError)
        assert issubclass(error, ValueError)
        with pytest.raises(CometRAGError):
            raise error("failure")
    assert issubclass(IngestionIncomplete, CometRAGError)
    assert issubclass(IngestionIncomplete, RuntimeError)
    assert not issubclass(ValueError, CometRAGError)
    assert not issubclass(asyncio.CancelledError, CometRAGError)
