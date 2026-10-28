from __future__ import annotations

from pathlib import Path

from comet_rag.engines.documents.mineru.structured import decode_json
from comet_rag.infrastructure.evidence.files import read_limited
from comet_rag.ports.capture import SourceLock


def load_source_locks(path: Path) -> tuple[SourceLock, ...]:
    """只读受控预检清单，不根据服务端full标志、文件名或请求metadata猜物理页数。

    清单权限与审批真实性由部署方负责；摘要绑定字节，不等于签名或人的独立复核。
    真正来源字节仍由采集器在POST前复制、限长和核验，避免预检后文件被替换。
    """
    value = decode_json(read_limited(path, 1024 * 1024))
    if (
        set(value) != {"version", "sources"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("来源锁清单版本或字段无效")
    rows = value["sources"]
    if not isinstance(rows, list) or not 0 < len(rows) <= 1000:
        raise ValueError("来源锁清单须有1..1000条")
    result = tuple(SourceLock.from_payload(row) for row in rows)
    if len({(r.kb_id, r.source_id) for r in result}) != len(result):
        raise ValueError("来源锁清单重复")
    return result
