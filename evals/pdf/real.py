from __future__ import annotations

from contextlib import AsyncExitStack
from hashlib import sha256
from pathlib import Path

import httpx

from comet_rag.composition.bootstrap import (
    build_embedding_model,
    build_model_gate,
    build_reranker,
)
from comet_rag.config.schemas import APPConfig
from comet_rag.services.retrieval import ReturnLevel, SearchMode
from evals.pdf.data import DATA, load
from evals.pdf.tokens import BodyTokens, body_budget
from evals.retrieval.baseline import Strategy, collect
from evals.retrieval.data import write_json
from evals.retrieval.metrics import Run, score


async def _counts(
    client: httpx.AsyncClient,
    model: str,
    run: Run,
    cache: dict[str, BodyTokens],
) -> dict[str, BodyTokens]:
    result = {}
    for query in run.results:
        for hit in query.hits:
            digest = sha256(hit.text.encode()).hexdigest()
            # 只缓存独立 raw text 编码，不从相同正文猜标题、身份或联合提示词。
            if digest not in cache:
                response = await client.post(
                    "tokenize", json={"model": model, "prompt": hit.text}
                )
                response.raise_for_status()
                cache[digest] = BodyTokens.from_response(hit.text, response.json())
            result[hit.id] = cache[digest]
    return result


async def _probe(client: httpx.AsyncClient, model: str) -> dict[str, str]:
    digest = sha256(model.encode())
    for text in ("A fixed token probe.", "云计算 PaaS 800-145。", "é\n表格 123"):
        response = await client.post("tokenize", json={"model": model, "prompt": text})
        response.raise_for_status()
        body = response.json()
        tokens = BodyTokens.from_response(text, body)
        digest.update(tokens.text_sha256.encode())
        digest.update(str(body["tokens"]).encode())
        digest.update(str(body.get("max_model_len")).encode())
    return {
        "model": model,
        "sha256": digest.hexdigest(),
        "scope": "observed raw-text behavior; not artifact revision",
    }


async def evaluate(
    config: APPConfig,
    output: Path,
    *,
    model_revision: str,
    data: Path = DATA,
) -> None:
    """原生文本 PDF 的固定比较；不创建生产解析器、应用 Context 或持久化索引。"""
    if not model_revision.strip():
        raise ValueError("必须显式声明权重版本；无法核实则填 unknown")
    dataset = load(data)
    settings = config.infrastructure_config.reranker
    if settings is None:
        raise ValueError("PDF 对照需要真实 reranker 和原生 tokenize 端点")
    output.mkdir(parents=True, exist_ok=False)
    summary = {}
    cache: dict[str, BodyTokens] = {}
    async with AsyncExitStack() as stack:
        model = build_embedding_model(config)
        stack.push_async_callback(model.close)
        reranker = build_reranker(config)
        if reranker is None:
            raise ValueError("PDF 对照未装配 reranker")
        stack.push_async_callback(reranker.close)
        gate = build_model_gate(config)
        model.bind_gate(gate)
        reranker.bind_gate(gate)
        counter = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=settings.base_url.rstrip("/").removesuffix("/v1") + "/",
                headers={
                    "Authorization": "Bearer " + (settings.api_key_value or "EMPTY")
                },
                timeout=30,
            )
        )
        before = await _probe(counter, settings.model_name)
        variants: tuple[tuple[Strategy, ReturnLevel], ...] = (
            ("recursive", ReturnLevel.CHILD),
            ("page", ReturnLevel.CHILD),
            ("hierarchy", ReturnLevel.CHILD),
            ("hierarchy", ReturnLevel.PARENT),
        )
        for strategy, level in variants:
            for use_reranker in (False, True):
                name = f"{strategy}-{level.value}-rerank-{str(use_reranker).lower()}"
                embedding = config.infrastructure_config.embedding_model
                run = await collect(
                    dataset,
                    strategy=strategy,
                    mode=SearchMode.HYBRID,
                    level=level,
                    embedding_model=model,
                    model_name=embedding.model_name,
                    embedding_dim=embedding.dim,
                    reranker=reranker,
                    rerank=use_reranker,
                    model_revision=model_revision,
                    child_size=800,
                    child_overlap=80,
                    parent_size=1600,
                )
                run.settings.update(
                    label_status="author provisional; independent human review pending",
                    extraction_scope="pypdf native text reference; not production MinerU",
                    reranker=settings.model_name if use_reranker else "disabled",
                    token_probe_sha256=before["sha256"],
                )
                counts = await _counts(counter, settings.model_name, run, cache)
                write_json(
                    output / f"{name}.tokens.json",
                    {
                        key: {
                            "text_sha256": value.text_sha256,
                            "count": value.count,
                        }
                        for key, value in counts.items()
                    },
                )
                for suffix, projected in [
                    ("top5", run),
                    *[
                        (
                            f"body{limit}",
                            body_budget(
                                run,
                                counts,
                                limit=limit,
                                tokenizer_model=settings.model_name,
                                tokenizer_revision="unknown",
                            ),
                        )
                        for limit in (512, 1024)
                    ],
                ]:
                    report = score(dataset, projected)
                    key = name + "-" + suffix
                    write_json(output / f"{key}.run.json", projected.model_dump())
                    write_json(output / f"{key}.report.json", report)
                    summary[key] = {k: v for k, v in report.items() if k != "per_query"}
                print(name, "completed", flush=True)
        after = await _probe(counter, settings.model_name)
        if before != after:
            raise ValueError("本轮原生文本 token 探针发生变化，不能发布同预算摘要")
        write_json(output / "token-probes.json", {"before": before, "after": after})
    write_json(output / "summary.json", summary)
