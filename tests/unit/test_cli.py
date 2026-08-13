"""`comet-rag` 命令行。

盯的是一个真实踩过的坑：`uvicorn comet_rag.api.main:app` 监听的是 uvicorn
默认的 `127.0.0.1:8000`，**而不是 config.yaml 里的 host/port**；
`python -m comet_rag.api.main` 才用配置里的值。同一份配置两条命令两个结果，
排查极费劲，因为"配置明明写了"。

`comet-rag serve` 就是为消除这个歧义而加的 —— 所以必须有用例钉住
"配置里的 host/port 真的被用上了"。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from comet_rag.cli import _mask, build_parser, main
from comet_rag.config.settings import ENV_CONFIG_PATH, get_config

CONFIG = {
    "server_config": {"app_name": "cli-test", "host": "10.1.2.3", "port": 9123},
    "infrastructure_config": {
        "embedding_model": {
            "base_url": "http://unused",
            "model_name": "stub",
            "api_key": "超级机密",
            "dim": 8,
        },
        "database": {
            "host": "localhost",
            "port": 5432,
            "username": "comet",
            "password": "不该被打出来",
            "database": "comet_rag",
        },
    },
}


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8")
    monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
    get_config.cache_clear()  # 进程级缓存，不清的话会串到别的用例
    yield path
    get_config.cache_clear()


# ── 参数解析 ───────────────────────────────────────────────────────────────


def test_subcommand_is_required() -> None:
    """裸跑 `comet-rag` 应当给出用法而不是默默什么都不做。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_worker_profile_is_constrained() -> None:
    """profile 打错字时立刻报错，而不是起一个消费空队列的 worker。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["worker", "embeder"])  # 少一个 d


# ── serve：配置里的 host/port 必须真的生效 ─────────────────────────────────


def test_serve_uses_host_and_port_from_config(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**这条就是加这个命令的全部理由。**

    `uvicorn comet_rag.api.main:app` 会用 127.0.0.1:8000 —— 因为 host/port
    属于服务器职责、不属于 ASGI app，配置里的值根本没机会参与。
    """
    captured: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        captured["app"] = app

    import uvicorn  # noqa: PLC0415

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert main(["serve", "--config", str(config_file)]) == 0

    assert captured["host"] == "10.1.2.3", "没有用配置里的 host"
    assert captured["port"] == 9123, "没有用配置里的 port"
    assert captured["app"] is not None


def test_explicit_flags_override_the_config(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令行 > 配置文件。容器化部署时常靠这个覆盖端口。"""
    captured: dict[str, Any] = {}
    import uvicorn  # noqa: PLC0415

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    bind_all = "0.0.0.0"  # noqa: S104 —— 容器化部署常用，这里只是断言它被透传
    main(["serve", "--config", str(config_file), "--host", bind_all, "--port", "1234"])

    assert captured["host"] == bind_all
    assert captured["port"] == 1234


def test_reload_mode_passes_an_import_string(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--reload` 下必须传导入字符串：uvicorn 要在**子进程**里重新 import，
    传 app 对象传不过去。配置路径则靠环境变量过去 —— 两件事必须同时成立。"""
    captured: dict[str, Any] = {}
    import uvicorn  # noqa: PLC0415

    def fake_run(app: Any, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    main(["serve", "--config", str(config_file), "--reload"])

    assert captured["app"] == "comet_rag.api.main:app", (
        "reload 模式传了 app 对象，子进程里会拿不到"
    )
    assert captured["reload"] is True
    assert os.environ[ENV_CONFIG_PATH] == str(config_file), (
        "配置路径没写进环境变量，子进程会退回去读 cwd 下的 config.yaml"
    )


def test_config_path_is_exported_for_child_processes(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 与 reload 都会另起子进程，只有环境变量传得过去。"""
    import uvicorn  # noqa: PLC0415

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    main(["serve", "--config", str(config_file)])
    assert os.environ[ENV_CONFIG_PATH] == str(config_file)


# ── worker ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "expected_lane"), [("preprocessor", "cpu"), ("embedder", "io")]
)
def test_worker_starts_the_right_profile(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    expected_lane: str,
) -> None:
    """起错 profile 的后果是"消费一条没人投递的队列"—— 不报错，只是永远空转。"""
    config = dict(CONFIG)
    config["infrastructure_config"] = {
        **CONFIG["infrastructure_config"],
        "redis": {"host": "localhost", "port": 6379},
    }
    config_file.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    get_config.cache_clear()

    captured: dict[str, Any] = {}
    import arq.worker  # noqa: PLC0415

    monkeypatch.setattr(
        arq.worker, "run_worker", lambda settings, **kw: captured.update(s=settings)
    )
    assert main(["worker", profile, "--config", str(config_file)]) == 0

    settings = captured["s"]
    assert settings.ctx["profile"].lane == expected_lane
    assert settings.queue_name.endswith(expected_lane)


# ── config 子命令：脱敏 ────────────────────────────────────────────────────


def test_mask_hides_secrets_recursively() -> None:
    masked = _mask(
        {"a": {"password": "p", "port": 1}, "b": [{"api_key": "k"}], "c": "ok"}
    )
    assert masked == {
        "a": {"password": "***", "port": 1},
        "b": [{"api_key": "***"}],
        "c": "ok",
    }


def test_config_command_never_prints_secrets(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """排查现场最容易顺手把密码打进工单里。宁可多脱敏几个字段。"""
    assert main(["config", "--config", str(config_file)]) == 0

    out = capsys.readouterr().out
    assert "不该被打出来" not in out, "密码被打出来了"
    assert "超级机密" not in out, "api_key 被打出来了"
    assert '"***"' in out

    body = json.loads(out.split("\n", 1)[1])
    assert body["server_config"]["port"] == 9123, "脱敏把正常字段也吃掉了"
