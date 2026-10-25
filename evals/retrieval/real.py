from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path

from comet_rag.composition.bootstrap import (
    build_embedding_model,
    build_model_gate,
    build_reranker,
)
from comet_rag.config.schemas import APPConfig
from comet_rag.config.settings import YamlConfigLoader
from comet_rag.services.retrieval import ReturnLevel, SearchMode
from evals.retrieval.baseline import collect
from evals.retrieval.data import DATA, generate, write_json
from evals.retrieval.metrics import score


async def evaluate_models(
    config_path: Path, output: Path, *, model_revision: str, data: Path = DATA
) -> None:
    """只创建模型客户端；配置中的数据库、队列、对象存储和 MinerU 均不装配。"""
    if not config_path.is_file():
        raise ValueError("必须提供存在的模型配置文件")
    config = YamlConfigLoader.get_config(str(config_path))
    await evaluate_configured_models(
        config, output, model_revision=model_revision, data=data
    )


async def evaluate_configured_models(
    config: APPConfig, output: Path, *, model_revision: str, data: Path = DATA
) -> None:
    """接收已校验模型配置，不要求将运行期凭据另存为 YAML。"""
    dataset = generate(output, data=data)
    summary: dict[str, object] = {}
    async with AsyncExitStack() as stack:
        model = build_embedding_model(config)
        stack.push_async_callback(model.close)
        reranker = build_reranker(config)
        if reranker is not None:
            stack.push_async_callback(reranker.close)
        gate = build_model_gate(config)
        model.bind_gate(gate)
        if reranker is not None:
            reranker.bind_gate(gate)
        embedding = config.infrastructure_config.embedding_model
        rerank_options = (False, True) if reranker is not None else (False,)
        for rerank in rerank_options:
            for mode in SearchMode:
                name = f"hierarchy-child-{mode.value}-rerank-{str(rerank).lower()}"
                run = await collect(
                    dataset,
                    strategy="hierarchy",
                    mode=mode,
                    level=ReturnLevel.CHILD,
                    embedding_model=model,
                    model_name=embedding.model_name,
                    embedding_dim=embedding.dim,
                    reranker=reranker,
                    rerank=rerank,
                    model_revision=model_revision,
                )
                settings = config.infrastructure_config.reranker
                run.settings["reranker"] = (
                    settings.model_name if rerank and settings else "disabled"
                )
                report = score(dataset, run)
                write_json(output / f"{name}.run.json", run.model_dump())
                write_json(output / f"{name}.report.json", report)
                summary[name] = {k: v for k, v in report.items() if k != "per_query"}
    write_json(output / "summary.json", summary)
    print(f"真实模型 / 合成语料 / InMemory 精确召回：{output / 'summary.json'}")
