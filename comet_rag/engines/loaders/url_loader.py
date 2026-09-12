from __future__ import annotations

import sys
from importlib import import_module

# URLLoader 的旧导入路径。

_target = import_module("comet_rag.infrastructure.sources.http")
sys.modules[__name__] = _target
