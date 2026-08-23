"""嵌入线路格式解码。"""

from __future__ import annotations

import base64
import struct

import pytest

from comet_rag.exceptions import CometRAGException
from comet_rag.infrastructure.providers._embedding_wire import decode_vector

VECTOR = [0.25, -0.5, 1.5]
ENCODED = base64.b64encode(struct.pack("<3f", *VECTOR)).decode()


def test_float_list_passes_through() -> None:
    assert decode_vector(VECTOR) is VECTOR


def test_base64_is_decoded_as_little_endian_float32() -> None:
    assert decode_vector(ENCODED) == VECTOR


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("注入感叹号", ENCODED[:4] + "!" + ENCODED[4:]),
        ("注入换行", ENCODED[:4] + "\n" + ENCODED[4:]),
        ("非 ASCII", ENCODED[:4] + "废" + ENCODED[4:]),
    ],
)
def test_corrupted_payload_is_rejected_not_silently_repaired(
    label: str, payload: str
) -> None:
    """**损坏的报文必须报错，不能悄悄"修好"。**

    `base64.b64decode` 默认丢弃非 base64 字符：往合法编码里插一个 `!`，它照样
    解得出四字节对齐的数据、照样通过长度检查，于是一条被损坏的报文变成一个
    看起来完全正常的向量 —— 没有异常、没有日志，只有检索结果慢慢变差。

    实测 `validate=False` 时前两种输入都返回 `[0.25, -0.5, 1.5]`，跟没坏一样。
    """
    with pytest.raises(CometRAGException, match="base64 向量无法解码"):
        decode_vector(payload)


def test_truncated_payload_is_rejected_by_the_length_check() -> None:
    with pytest.raises(CometRAGException, match="不是 4 的倍数"):
        decode_vector(ENCODED[: len(ENCODED) // 2])
