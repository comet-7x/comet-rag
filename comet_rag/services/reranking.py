from __future__ import annotations

import math

from comet_rag.ports import RankedDocument


class InvalidRerankResult(ValueError):
    """固定诊断原因，避免把供应商响应内容带入查询结果。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def validate_ranking(result: object, *, candidate_count: int) -> list[RankedDocument]:
    """重排必须返回全部候选的排列，且每个分数都能安全表示为 JSON 数字。"""
    if not isinstance(result, list):
        raise InvalidRerankResult("invalid_result")
    if len(result) != candidate_count:
        raise InvalidRerankResult("result_count_mismatch")

    ranked: list[RankedDocument] = []
    for item in result:
        if not isinstance(item, RankedDocument):
            raise InvalidRerankResult("invalid_result")
        # bool/浮点数与整数相等，不代表它们是可信的候选索引。
        if not isinstance(item.index, int) or isinstance(item.index, bool):
            raise InvalidRerankResult("result_index_mismatch")
        ranked.append(item)
    if sorted(item.index for item in ranked) != list(range(candidate_count)):
        raise InvalidRerankResult("result_index_mismatch")

    for item in ranked:
        if not isinstance(item.score, (int, float)) or isinstance(item.score, bool):
            raise InvalidRerankResult("result_score_invalid")
        try:
            finite = math.isfinite(item.score)
        except OverflowError:
            finite = False
        if not finite:
            raise InvalidRerankResult("result_score_invalid")
    return ranked
