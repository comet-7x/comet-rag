"""全局测试夹具。

分层约定（见 tasks/spec.md §6）：
  tests/unit/         无外部依赖，全套必须 < 10s。默认只跑这一层。
  tests/integration/  需 docker-compose 起中间件，标 @pytest.mark.integration
  tests/e2e/          端到端链路，标 @pytest.mark.e2e
  tests/benchmark/    性能基线，标 @pytest.mark.benchmark

硬性要求：unit 层不允许打真实网络，也不允许依赖 GPU 服务。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).parent


@pytest.fixture(autouse=True)
def _isolate_pipeline_hooks() -> Iterator[None]:
    """每个用例结束后还原 `PipelineHooks` 注册表。

    注册表是进程级全局的，不隔离的话 A 用例注册的 hook 会跑进 B 用例，
    而且是否踩雷取决于用例执行顺序 —— 这类 bug 排查起来极其费时。
    autouse 而非按需，是因为"忘了加"正是这类污染的主要来源。
    """
    from comet_rag.engines.pipelines import PipelineHooks

    state = PipelineHooks.snapshot()
    try:
        yield
    finally:
        PipelineHooks.restore(state)


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """测试样本文件根目录（docx 样本、快照等）。"""
    return TESTS_ROOT / "fixtures"


@pytest.fixture
def anyio_backend() -> str:
    """若将来引入 anyio 风格的测试，固定用 asyncio 后端。"""
    return "asyncio"
