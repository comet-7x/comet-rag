from __future__ import annotations

import sys
from importlib import import_module

# OMML 符号表的旧导入路径。

_target = import_module("comet_rag.engines.documents.docx.latex_dict")
sys.modules[__name__] = _target
