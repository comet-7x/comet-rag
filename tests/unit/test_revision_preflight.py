from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

from comet_rag.cli import main
from comet_rag.composition.revision_preflight import inspect_revision_deployment
from comet_rag.config.schemas import APPConfig, Backend
from comet_rag.engines.pipelines import PipelineConfig
from tests.unit.test_revision_ingestion_wiring import writing_config


class Deployments(TypedDict):
    previous: APPConfig
    api: APPConfig
    preprocessor: APPConfig
    embedder: APPConfig


def deployments() -> Deployments:
    target = writing_config()
    data = target.model_dump(mode="python")
    data["revision_ingestion"]["enabled"] = False
    data["revision_reconciliation"]["enabled"] = False
    data["ingest_policy"]["accept_new_tasks"] = False
    data["infrastructure_config"]["database"]["database"] = "legacy_db"
    data["infrastructure_config"]["redis"]["db_index"] = 15
    data["infrastructure_config"]["vector_database"]["collection_prefix"] = "legacy_"
    return {
        "previous": APPConfig.model_validate(data),
        "api": target,
        "preprocessor": target.model_copy(deep=True),
        "embedder": target.model_copy(deep=True),
    }


def test_preflight_describes_only_configuration_evidence():
    result = inspect_revision_deployment(**deployments())
    assert result.passed
    report = result.to_dict()
    assert report["scope"] == "configuration-only"
    assert isinstance(report["unverified"], list) and len(report["unverified"]) == 4
    assert len(set(result.bindings.values())) == 1
    assert result.bindings["api"] is not None


@pytest.mark.parametrize(
    "resource,field",
    [
        ("database", "database"),
        ("redis", "db_index"),
        ("vector_database", "collection_prefix"),
    ],
)
def test_alias_endpoint_cannot_hide_reused_logical_namespace(resource, field):
    configs = deployments()
    old = configs["previous"].infrastructure_config
    new = configs["api"].infrastructure_config
    setattr(getattr(old, resource), field, getattr(getattr(new, resource), field))
    if resource == "vector_database":
        assert old.vector_database is not None
        old.vector_database.endpoint = "http://different-alias:19530"
    else:
        getattr(old, resource).host = "different-alias"
    result = inspect_revision_deployment(**configs)
    assert not result.passed
    expected = {"database": "postgres", "redis": "redis", "vector_database": "milvus"}[
        resource
    ]
    assert not next(
        c for c in result.checks if c.name == f"namespace.{expected}"
    ).passed


@pytest.mark.parametrize("role", ["api", "preprocessor", "embedder"])
@pytest.mark.parametrize("change", ["model", "limit", "disabled", "parent"])
def test_every_role_requires_the_same_enabled_write_plan(role, change):
    configs = deployments()
    config = configs[role]
    if change == "model":
        config.infrastructure_config.embedding_model.model_name = "different-model"
    elif change == "limit":
        config.limits.embed_batch_size += 1
    elif change == "disabled":
        config.revision_ingestion.enabled = False
    else:
        assert config.revision_ingestion.parent_chunk_size is not None
        config.revision_ingestion.parent_chunk_size += 1
    result = inspect_revision_deployment(**configs)
    assert not result.passed
    assert any(
        not c.passed and c.name.endswith("writer_binding") for c in result.checks
    )


def test_unpaused_source_and_memory_source_fail():
    configs = deployments()
    old = configs["previous"]
    old.ingest_policy.accept_new_tasks = True
    old.backends.task_store = Backend.MEMORY
    old.backends.task_executor = Backend.INPROCESS
    old.retrieval.revision_aware = False
    result = inspect_revision_deployment(**configs)
    assert not result.passed
    assert {c.name for c in result.checks if not c.passed} == {
        "previous.admission_paused",
        "previous.persistent",
    }


def test_mutated_invalid_plan_is_reported_without_echoing_input():
    configs = deployments()
    configs["embedder"].revision_ingestion.parent_chunk_size = 1
    result = inspect_revision_deployment(**configs)
    assert not result.passed
    assert "embedder.valid" in {c.name for c in result.checks if not c.passed}


def test_programmatic_overrides_are_checked_for_every_role():
    configs = deployments()
    pipelines = {
        role: PipelineConfig(chunk_size=100, chunk_overlap=0)
        for role in ("api", "preprocessor", "embedder")
    }
    assert inspect_revision_deployment(**configs, pipelines=pipelines).passed
    pipelines["embedder"].chunk_size = 200
    result = inspect_revision_deployment(**configs, pipelines=pipelines)
    assert not result.passed
    assert not next(
        c for c in result.checks if c.name == "embedder.writer_binding"
    ).passed


def test_partial_programmatic_plan_is_not_replaced_with_defaults():
    result = inspect_revision_deployment(
        **deployments(), pipelines={"api": PipelineConfig()}
    )
    assert not result.passed and result.checks[0].name == "pipelines.complete"


def test_equal_disabled_bindings_do_not_approve_legacy_writers():
    configs = deployments()
    for role in ("api", "preprocessor", "embedder"):
        configs[role].revision_ingestion.enabled = False
    result = inspect_revision_deployment(**configs)
    assert not result.passed
    assert {c.name for c in result.checks if not c.passed} == {
        f"{role}.writer_binding" for role in ("api", "preprocessor", "embedder")
    }


def test_rotated_credentials_do_not_change_binding_or_appear_in_report():
    configs = deployments()
    raw = configs["embedder"].model_dump(mode="python")
    raw["infrastructure_config"]["database"]["password"] = "rotated-private-password"  # noqa: S105
    configs["embedder"] = APPConfig.model_validate(raw)
    result = inspect_revision_deployment(**configs)
    assert result.passed
    assert "rotated-private-password" not in json.dumps(result.to_dict())


def write_configs(tmp_path: Path):
    configs = deployments()
    arguments = ["revision-preflight"]
    for role, config in configs.items():
        assert isinstance(config, APPConfig)
        raw = config.model_dump(mode="json")
        # 使用明文夹具确认 CLI 从未输出校验输入或配置全文。
        raw["infrastructure_config"]["database"]["password"] = (
            "private-preflight-password"  # noqa: S105
        )
        path = tmp_path / f"{role}.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        arguments.extend([f"--{role}-config", str(path)])
    return arguments


def test_cli_preflight_has_no_resource_construction_or_environment_changes(
    tmp_path, capsys, monkeypatch
):
    from comet_rag.composition import bootstrap

    def unexpected(*args, **kwargs):
        raise AssertionError("offline command opened resources")

    monkeypatch.setattr(bootstrap, "build_context", unexpected)
    monkeypatch.setattr(socket, "create_connection", unexpected)
    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    arguments = write_configs(tmp_path)
    before = dict(os.environ)
    assert main(arguments) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["passed"] is True
    assert "private-preflight-password" not in output
    assert dict(os.environ) == before


@pytest.mark.parametrize("invalid", ["missing", "yaml", "schema", "empty"])
def test_cli_config_failure_is_nonzero_and_never_echoes_secrets(
    tmp_path, capsys, invalid, monkeypatch
):
    arguments = write_configs(tmp_path)
    path = tmp_path / "embedder.yaml"
    if invalid == "missing":
        path.unlink()
    elif invalid == "yaml":
        path.write_text("private-preflight-password: [", encoding="utf-8")
    elif invalid == "empty":
        monkeypatch.setenv("COMET_RAG_CONFIG", str(path))
        arguments[-1] = ""
    else:
        path.write_text(
            "infrastructure_config: private-preflight-password", encoding="utf-8"
        )
    assert main(arguments) == 2
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "passed": False,
        "invalid_config": "embedder",
    }


def test_cli_constraint_failure_is_nonzero_with_stable_check_names(tmp_path, capsys):
    arguments = write_configs(tmp_path)
    path = tmp_path / "previous.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["ingest_policy"]["accept_new_tasks"] = True
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert main(arguments) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert {c["name"] for c in report["checks"] if not c["passed"]} == {
        "previous.admission_paused"
    }
