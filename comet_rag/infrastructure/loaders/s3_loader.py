from __future__ import annotations

import sys
from importlib import import_module

# S3Loader 的旧导入路径。

_target = import_module("comet_rag.infrastructure.sources.s3")
sys.modules[__name__] = _target
