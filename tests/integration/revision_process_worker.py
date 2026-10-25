"""生产装配的独立进程演练入口；文件屏障只用于可重复地停在外部提交边界。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from contextlib import suppress
from pathlib import Path

from arq.worker import create_worker

from comet_rag.config.settings import YamlConfigLoader
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.infrastructure.models.embedding.base import AsyncBaseEmbeddingModel
from comet_rag.workers.base import build_settings
from comet_rag.workers.embedder import PROFILE as IO
from comet_rag.workers.maintenance import sweep_stale_tasks
from comet_rag.workers.preprocessor import PROFILE as CPU


class Events:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def emit(self, name: str, **values: object) -> None:
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"event": name, "pid": os.getpid(), **values}) + "\n"
            )

    async def barrier(self, revision: str) -> None:
        self.emit("barrier", revision=revision)
        while not (self.directory / "release").exists():
            await asyncio.sleep(0.01)


class Embedding(AsyncBaseEmbeddingModel):
    def __init__(self, events: Events) -> None:
        self.events = events
        self.calls = 0

    async def _embed(self, data, **kwargs) -> list[float]:
        self.calls += 1
        self.events.emit("embedding", calls=self.calls)
        return [1.0, 0.0]

    async def close(self) -> None:
        self.events.emit("model_closed", calls=self.calls)


async def run(args: argparse.Namespace) -> None:
    events = Events(Path(args.directory))
    config = YamlConfigLoader.get_config(args.config)
    settings = build_settings(
        CPU if args.lane == "cpu" else IO,
        config=config,
        embedding_model=Embedding(events),
        pipeline_config=PipelineConfig(
            chunk_size=10, chunk_overlap=0, embed_batch_size=2, max_concurrency=2
        ),
    )
    if args.grace is not None:
        settings.job_completion_wait = args.grace
    startup, shutdown = settings.on_startup, settings.on_shutdown

    async def initialize(ctx):
        await startup(ctx)
        context = ctx["context"]
        if args.barrier != "none":
            activate = context.document_store.activate_revision

            async def blocked(key):
                if args.barrier == "before-activation":
                    await events.barrier(key.revision_id)
                outcome = await activate(key)
                if args.barrier == "after-activation":
                    await events.barrier(key.revision_id)
                return outcome

            context.document_store.activate_revision = blocked
        if args.recover:
            events.emit("recovered", requeued=await sweep_stale_tasks(ctx))
        events.emit("ready", worker=context.task_executor.worker_id)

    async def close(ctx):
        events.emit("closing")
        await shutdown(ctx)
        events.emit("closed")

    settings.on_startup = staticmethod(initialize)
    settings.on_shutdown = staticmethod(close)
    worker = create_worker(
        settings, max_jobs=1, poll_delay=0.01, keep_result=0, log_results=False
    )

    async def observe_stop():
        while worker.allow_pick_jobs:
            await asyncio.sleep(0.005)
        events.emit("stopped_picking")

    observation = asyncio.create_task(observe_stop())
    try:
        # 原生信号取消轮询；SDK close 仍排干并触发资源关闭。
        with suppress(asyncio.CancelledError):
            await worker.async_run()
    finally:
        observation.cancel()
        await asyncio.gather(observation, return_exceptions=True)
        await worker.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--lane", choices=("cpu", "io"), required=True)
    parser.add_argument(
        "--barrier",
        choices=("none", "before-activation", "after-activation"),
        default="none",
    )
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--grace", type=int)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
