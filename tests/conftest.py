"""全局测试夹具。

分层约定（见 tasks/spec.md §6）：
  tests/unit/         无外部依赖，全套必须 < 10s。默认只跑这一层。
  tests/integration/  需 docker-compose 起中间件，标 @pytest.mark.integration
  tests/e2e/          端到端链路，标 @pytest.mark.e2e
  tests/benchmark/    性能基线，标 @pytest.mark.benchmark

硬性要求：unit 层不允许打真实网络，也不允许依赖 GPU 服务。
"""

from __future__ import annotations

from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).parent


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """测试样本文件根目录（docx 样本、快照等）。"""
    return TESTS_ROOT / "fixtures"


@pytest.fixture
def anyio_backend() -> str:
    """若将来引入 anyio 风格的测试，固定用 asyncio 后端。"""
    return "asyncio"
