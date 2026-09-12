from __future__ import annotations

import sys
from importlib import import_module

# OMML 转换器的旧导入路径。

_target = import_module("comet_rag.engines.documents.docx.omml")
sys.modules[__name__] = _target
