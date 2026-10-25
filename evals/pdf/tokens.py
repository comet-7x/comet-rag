from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from evals.retrieval.metrics import Run


@dataclass(frozen=True, slots=True)
class BodyTokens:
    text_sha256: str
    count: int

    @classmethod
    def from_response(cls, text: str, response: object) -> BodyTokens:
        if not isinstance(response, dict):
            raise ValueError("tokenize 响应必须是对象")
        count, tokens = response.get("count"), response.get("tokens")
        if type(count) is not int or count < 0 or not isinstance(tokens, list):
            raise ValueError("token 数必须为非负整数")
        if count != len(tokens) or any(type(t) is not int or t < 0 for t in tokens):
            raise ValueError("token 数与实际序列不一致")
        if text and count == 0:
            raise ValueError("非空正文不能报告零 token")
        return cls(sha256(text.encode()).hexdigest(), count)


def body_budget(
    run: Run,
    counts: Mapping[str, BodyTokens],
    *,
    limit: int,
    tokenizer_model: str,
    tokenizer_revision: str,
) -> Run:
    """只取原排序的完整前缀；预算是独立正文计数之和，不是服务端联合提示词。"""
    if type(limit) is not int or limit <= 0:
        raise ValueError("正文预算必须为正整数")
    if not tokenizer_model.strip() or not tokenizer_revision.strip():
        raise ValueError("必须显式声明计数模型与 revision；不能确认时填 unknown")
    result = run.model_copy(deep=True)
    for query in result.results:
        used, kept = 0, []
        for hit in query.hits:
            observation = counts[hit.id]
            if observation.text_sha256 != sha256(hit.text.encode()).hexdigest():
                raise ValueError("token 观测与原文身份不符")
            if type(observation.count) is not int or observation.count < 1:
                raise ValueError("非空正文的 token 观测无效")
            if used + observation.count > limit:
                break
            kept.append(hit)
            used += observation.count
        query.hits = kept
        # 原 search 时延和预算后的返回结果不再是同一个操作，避免伪造预算检索延迟。
        query.elapsed_ms = None
    result.settings.update(
        body_token_limit=limit,
        budget_scope="sum of separately tokenized returned bodies; whole ranked prefix",
        tokenizer_model=tokenizer_model,
        tokenizer_revision=tokenizer_revision,
        timing_scope="not measured for budget projection",
    )
    return result
