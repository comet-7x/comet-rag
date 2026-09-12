from __future__ import annotations

import sys
from importlib import import_module

# ARQ 执行器的旧导入路径。

_target = import_module("comet_rag.infrastructure.task_execution.arq")
sys.modules[__name__] = _target
