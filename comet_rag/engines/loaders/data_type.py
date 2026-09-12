from __future__ import annotations

import sys
from importlib import import_module

# 文档格式类型的旧导入路径。

_target = import_module("comet_rag.engines.documents.formats")
sys.modules[__name__] = _target
