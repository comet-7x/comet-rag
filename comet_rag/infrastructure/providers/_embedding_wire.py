"""OpenAI 兼容嵌入协议的线路格式处理。

`encoding_format=base64` 是协议里的**传输优化**（报文小一半），不是调用方该
看到的东西。就地解回浮点数组，否则 `embed_query` 声明返回 `list[float]`、
实际给出 `str` —— 契约在说谎，而且只在配了 base64 的部署上才炸。

放在共享位置是因为 Qwen 与 OpenAI 两个适配器讲的是同一套协议：先前只在
Qwen 里解，OpenAI 那边同样的洞就一直开着（评审指出）。
"""

from __future__ import annotations

import base64
import struct

from comet_rag.exceptions import CometRAGException


def decode_vector(embedding: list[float] | str) -> list[float]:
    """把一条向量还原成浮点数组；已经是数组的原样返回。"""
    if not isinstance(embedding, str):
        return embedding
    raw = base64.b64decode(embedding)
    if len(raw) % 4:
        raise CometRAGException(
            f"base64 向量长度 {len(raw)} 不是 4 的倍数，无法按 float32 解码"
        )
    # 显式小端 float32：OpenAI 协议如此规定，不能依赖本机字节序
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


__all__ = ["decode_vector"]
