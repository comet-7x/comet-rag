from __future__ import annotations

import sys
from importlib import import_module

# DOCX 提取器的旧导入路径。

_target = import_module("comet_rag.engines.documents.docx.extractor")
sys.modules[__name__] = _target
