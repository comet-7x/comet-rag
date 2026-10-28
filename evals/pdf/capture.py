from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

from comet_rag.core.concurrency import Gate
from comet_rag.engines.documents.mineru.structured import decode_json
from comet_rag.infrastructure.evidence.files import read_limited
from comet_rag.infrastructure.extractors._mineru_v1_protocol import (
    CaptureError,
    CaptureSource,
    digest,
    integer,
)
from comet_rag.infrastructure.extractors.mineru_v1 import AsyncMinerUV1Collector


async def main_async(args: argparse.Namespace) -> None:
    """独立进程只构造一份客户端及有界闸门，不读取或修改服务config.yaml。"""
    lock = decode_json(read_limited(args.source_lock, 1024 * 1024))
    samples = lock.get("samples")
    if not isinstance(samples, list):
        raise ValueError("来源锁缺少samples")
    matches = [s for s in samples if isinstance(s, dict) and s.get("id") == args.sample]
    if len(matches) != 1:
        raise ValueError("来源锁必须唯一列出所选PDF")
    sample = matches[0]
    if not isinstance(sample.get("path"), str):
        raise ValueError("来源路径必须是字符串")
    source = CaptureSource(
        Path(sample["path"]),
        args.sample + ".pdf",
        digest(sample.get("sha256")),
        integer(sample.get("bytes")),
        integer(sample.get("pages")),
    )
    headers = {}
    if args.api_key_env:
        token = os.environ.get(args.api_key_env)
        if not token:
            raise ValueError("指定的凭据环境变量未设置")
        headers["Authorization"] = "Bearer " + token
    gate = Gate(limit=2, max_waiting=16, acquire_timeout=30, name="mineru-v1-capture")
    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    async with httpx.AsyncClient(
        headers=headers, transport=transport, trust_env=False, follow_redirects=False
    ) as client:
        collector = AsyncMinerUV1Collector(args.url, client=client, gate=gate)
        result = await collector.collect(source, args.out)
    print(json.dumps({k: result[k] for k in ("outcome", "task_id", "received_bytes")}))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="独立MinerU4.0.12 V1采集，不接入生产入库"
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--source-lock", required=True, type=Path)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--api-key-env", help="可选凭据环境变量名，不在命令行传令牌")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except CaptureError as exc:
        print(json.dumps({"error": exc.code, "observed_task_id": exc.task_id}))
        raise SystemExit(1) from None
    except (ValueError, OSError):
        print(json.dumps({"error": "invalid_local_input"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
