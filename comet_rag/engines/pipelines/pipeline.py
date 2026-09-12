from __future__ import annotations

import sys
from importlib import import_module

# 可装配 Pipeline 的旧导入路径。

_target = import_module("comet_rag.pipeline")
sys.modules[__name__] = _target
