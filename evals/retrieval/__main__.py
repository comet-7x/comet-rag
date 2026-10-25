from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from comet_rag.services.retrieval import ReturnLevel, SearchMode
from evals.retrieval.baseline import collect
from evals.retrieval.data import generate, read_json, write_json
from evals.retrieval.metrics import Run, score


async def baseline(output: Path) -> None:
    dataset = generate(output)
    summary: dict[str, object] = {}
    for strategy in ("recursive", "markdown", "hierarchy"):
        levels = (
            (ReturnLevel.CHILD, ReturnLevel.PARENT)
            if strategy == "hierarchy"
            else (ReturnLevel.CHILD,)
        )
        for level in levels:
            for mode in SearchMode:
                name = f"{strategy}-{level.value}-{mode.value}"
                run = await collect(dataset, strategy=strategy, mode=mode, level=level)
                report = score(dataset, run)
                write_json(output / f"{name}.run.json", run.model_dump())
                write_json(output / f"{name}.report.json", report)
                summary[name] = {k: v for k, v in report.items() if k != "per_query"}
    write_json(output / "summary.json", summary)
    print(f"离线回归完成（非真实模型质量）：{output / 'summary.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="固定语料与检索质量评分工具（仓库工具，不随库安装）"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("generate", "baseline"):
        child = sub.add_parser(command)
        child.add_argument("--out", type=Path, required=True, help="不存在的新目录")
    scoring = sub.add_parser("score")
    scoring.add_argument("--run", type=Path, required=True)
    real = sub.add_parser("real", help="仅访问配置中的模型服务，使用独立内存索引")
    real.add_argument("--config", type=Path, required=True)
    real.add_argument("--out", type=Path, required=True)
    real.add_argument(
        "--model-revision",
        required=True,
        help="已部署权重/版本标识；不能确认时显式填 unknown",
    )
    args = parser.parse_args()
    if args.command == "score":
        with tempfile.TemporaryDirectory() as tmp:
            dataset = generate(Path(tmp) / "corpus")
            report = score(dataset, read_json(args.run, Run))
            import json

            print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    elif args.command == "generate":
        dataset = generate(args.out)
        print(f"dataset_sha256={dataset.fingerprint}")
    elif args.command == "real":
        from evals.retrieval.real import evaluate_models

        asyncio.run(
            evaluate_models(args.config, args.out, model_revision=args.model_revision)
        )
    else:
        asyncio.run(baseline(args.out))


if __name__ == "__main__":
    main()
