from __future__ import annotations

import sys
from importlib import import_module

# 来源文件工具的旧导入路径。

_target = import_module("comet_rag.infrastructure.sources.file_info")
sys.modules[__name__] = _target
