"""`comet-rag` 命令行：**唯一推荐的启动入口**。

## 为什么需要它（这是个真实的坑）

`uvicorn comet_rag.api.main:app` 起出来的服务，监听的是 uvicorn 默认的
`127.0.0.1:8000`，**而不是 config.yaml 里写的 host/port**。
`python -m comet_rag.api.main` 才会用配置里的值。同一份配置、两条命令、
两个结果 —— 排查起来非常费劲，因为"配置明明写了"。

根因不是 bug，是职责划分：host/port 属于**服务器**，不属于 ASGI 应用。
uvicorn 命令行直接拿走了 app，配置里的那两个值根本没有机会参与。

所以这里把"读配置 → 起服务器"合成一条命令。`uvicorn` 那条路留给需要
`--reload` 的开发场景，并在 `docs/deployment.md` 里写明它的行为差异。

## 子命令

    comet-rag serve                    # API 进程（生产端）
    comet-rag worker preprocessor      # CPU 密集，靠加进程扩容
    comet-rag worker embedder          # IO 密集，靠加并发扩容
    comet-rag config                   # 打印生效配置（脱敏），排查用

`--config` 会同时写进环境变量 `COMET_RAG_CONFIG`：`--reload` 与 worker 都会
另起子进程，只有环境变量传得过去。

用 argparse 而非 click/typer：CLI 属于核心包，不该为它多一个依赖 ——
`engines/` 那条"当库用"的路径本来就以依赖少为卖点（spec A1）。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from comet_rag.config.settings import ENV_CONFIG_PATH, get_config, resolve_config_path


def _add_config_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=f"配置文件路径（默认取 ${ENV_CONFIG_PATH}，再默认 ./config.yaml）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="comet-rag", description="Comet-RAG：RAG 全流程服务"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 API 服务（生产端）")
    _add_config_option(serve)
    serve.add_argument("--host", help="覆盖配置里的监听地址")
    serve.add_argument("--port", type=int, help="覆盖配置里的监听端口")
    serve.add_argument(
        "--reload",
        action="store_true",
        help="改动即重启（仅开发用；会另起子进程，故配置路径经环境变量传递）",
    )
    serve.add_argument("--log-level", default="info")

    worker = sub.add_parser("worker", help="启动消费端 worker")
    worker.add_argument(
        "profile",
        choices=("preprocessor", "embedder"),
        help="preprocessor=CPU 密集（加进程扩）；embedder=IO 密集（加并发扩）",
    )
    _add_config_option(worker)

    show = sub.add_parser("config", help="打印生效配置（密码已脱敏）")
    _add_config_option(show)

    return parser


def _apply_config_path(path: str | None) -> str:
    """把配置路径固化到环境变量，子进程才拿得到。"""
    resolved = resolve_config_path(path)
    os.environ[ENV_CONFIG_PATH] = resolved
    return resolved


# ── 子命令实现 ─────────────────────────────────────────────────────────────


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn  # noqa: PLC0415 —— server extra 才有，核心安装不该被它拖累

    _apply_config_path(args.config)
    config = get_config()
    host = args.host or config.server_config.host
    port = args.port if args.port is not None else config.server_config.port

    if args.reload:
        # reload 模式必须给导入字符串：uvicorn 要在子进程里重新 import 应用，
        # 传 app 对象是传不过去的。配置路径靠上面写好的环境变量过去。
        uvicorn.run(
            "comet_rag.api.main:app",
            host=host,
            port=port,
            reload=True,
            log_level=args.log_level,
        )
        return 0

    from comet_rag.api.main import create_app  # noqa: PLC0415

    uvicorn.run(create_app(config), host=host, port=port, log_level=args.log_level)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from arq.worker import run_worker  # noqa: PLC0415

    _apply_config_path(args.config)
    from comet_rag.workers.base import build_settings  # noqa: PLC0415

    if args.profile == "preprocessor":
        from comet_rag.workers.preprocessor import PROFILE  # noqa: PLC0415
    else:
        from comet_rag.workers.embedder import PROFILE  # noqa: PLC0415

    run_worker(build_settings(PROFILE))  # type: ignore[arg-type]
    return 0


#: 这些字段一律不打印明文。宁可多脱敏几个，也别在排查现场把密码打进日志。
_SECRET_KEYS = frozenset({"password", "api_key", "secret_key", "access_key", "token"})


def _mask(value: Any, key: str = "") -> Any:
    if key in _SECRET_KEYS and value:
        return "***"
    if isinstance(value, dict):
        return {k: _mask(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def cmd_config(args: argparse.Namespace) -> int:
    import json  # noqa: PLC0415

    path = _apply_config_path(args.config)
    config = get_config()
    print(f"# 配置来源：{path}")  # noqa: T201 —— 这个子命令的产出就是给人看的
    print(
        json.dumps(_mask(config.model_dump(mode="json")), indent=2, ensure_ascii=False)
    )  # noqa: T201
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"serve": cmd_serve, "worker": cmd_worker, "config": cmd_config}
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
