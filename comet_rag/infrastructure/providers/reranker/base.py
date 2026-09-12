from __future__ import annotations

import sys
from importlib import import_module

_target = import_module("comet_rag.infrastructure.models.reranker.base")
sys.modules[__name__] = _target
