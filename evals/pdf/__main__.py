from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from evals.pdf.data import DATA, blind_packet, load


def main() -> None:
    parser = argparse.ArgumentParser(
        description="真实 PDF 参考快照与人工盲审包；不作生产解析"
    )
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--review-out", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--model-revision", default="unknown")
    args = parser.parse_args()
    if (args.config is None) != (args.out is None):
        parser.error("--config 和 --out 必须同时提供")
    if args.config is not None:
        from comet_rag.config.settings import YamlConfigLoader
        from evals.pdf.real import evaluate

        asyncio.run(
            evaluate(
                YamlConfigLoader.get_config(str(args.config)),
                args.out,
                model_revision=args.model_revision,
                data=args.data,
            )
        )
    if args.review_out is not None:
        blind_packet(args.review_out, data=args.data)
    print(f"dataset_sha256={load(args.data).fingerprint}; independent review=pending")


if __name__ == "__main__":
    main()
