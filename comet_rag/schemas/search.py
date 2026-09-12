from __future__ import annotations

import sys
from importlib import import_module

# 检索 API DTO 的旧导入路径。

_target = import_module("comet_rag.api.schemas.search")
sys.modules[__name__] = _target
