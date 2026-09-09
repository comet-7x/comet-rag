"""OpenAI 兼容嵌入响应的线路格式处理。"""

from __future__ import annotations

import base64
import binascii
import struct

from comet_rag.exceptions import CometRAGException


def decode_vector(embedding: list[float] | str) -> list[float]:
    """把一条向量还原成浮点数组；已经是数组的原样返回。

    ``validate=True`` 不是可选的。默认的 `b64decode` 会**静默丢弃**非 base64
    字符：往合法编码里插一个 `!` 或换行，它照样解得出四字节对齐的数据、照样
    通过下面的长度检查，于是一条被损坏的报文变成一个看起来完全正常的向量 ——
    损坏报文会伪装成正常向量，导致检索质量静默下降。

    实测（`AACAPgAAAL8AAMA/` 插入 `!`）：

        validate=False → [0.25, -0.5, 1.5]     ← 悄悄"修好"了
        validate=True  → 拒绝：Only base64 data is allowed
    """
    if not isinstance(embedding, str):
        return embedding
    try:
        raw = base64.b64decode(embedding, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CometRAGException(f"base64 向量无法解码：{exc}") from exc
    if len(raw) % 4:
        raise CometRAGException(
            f"base64 向量长度 {len(raw)} 不是 4 的倍数，无法按 float32 解码"
        )
    # 显式小端 float32：OpenAI 协议如此规定，不能依赖本机字节序
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


__all__ = ["decode_vector"]
