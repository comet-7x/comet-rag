from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from comet_rag.composition.pipeline_config import resolve_pipeline_config
from comet_rag.composition.revision_config import revision_binding
from comet_rag.config.schemas import APPConfig, Backend
from comet_rag.engines.pipelines import PipelineConfig


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RevisionPreflight:
    """仅证明输入配置的约束；不授权迁移、切流或删除旧端。"""

    checks: tuple[PreflightCheck, ...]
    bindings: dict[str, str | None]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "scope": "configuration-only",
            "passed": self.passed,
            "checks": [asdict(check) for check in self.checks],
            "bindings": self.bindings,
            "unverified": [
                "运行实例是否使用这些配置、代码及被核验的 Pipeline",
                "远端存储隔离、schema/索引、权限、模型实际名称/维度与可用性",
                "旧入口及远端写入排干、资料完整性、重建与查询质量",
                "路由切换、切换后增量与回退数据完整性",
            ],
        }


def inspect_revision_deployment(
    *,
    previous: APPConfig,
    api: APPConfig,
    preprocessor: APPConfig,
    embedder: APPConfig,
    pipelines: Mapping[str, PipelineConfig] | None = None,
) -> RevisionPreflight:
    """离线核验 CLI 部署；仅接受不同逻辑命名空间，主机别名不能绕过名称检查。

    初版有意要求新 SQL database、新 Redis DB index，以及新 Milvus database 或
    prefix；不同服务器复用同名资源需额外人工证明，不由地址字符串替代。不同名称
    仍不能证明代理未重写路由。CLI 使用默认 Pipeline；程序化调用必须同时传三角色
    的覆盖设置，仍不核验资源注入对象。
    """
    checks: list[PreflightCheck] = []
    bindings: dict[str, str | None] = {}
    if pipelines is not None and set(pipelines) != {"api", "preprocessor", "embedder"}:
        return RevisionPreflight(
            (
                PreflightCheck(
                    "pipelines.complete", False, "需同时提供三个角色的 Pipeline"
                ),
            ),
            {},
        )
    configurations: dict[str, APPConfig] = {}
    for role, candidate in (
        ("previous", previous),
        ("api", api),
        ("preprocessor", preprocessor),
        ("embedder", embedder),
    ):
        try:
            config = APPConfig.model_validate(candidate.model_dump(mode="python"))
            configurations[role] = config
            if role != "previous":
                pipeline = resolve_pipeline_config(
                    config.limits, None if pipelines is None else pipelines[role]
                )
                bindings[role] = revision_binding(config, pipeline)
        except (ValueError, TypeError):
            # 校验异常可能携带输入凭据；报告只给角色和稳定检查名。
            checks.append(PreflightCheck(f"{role}.valid", False, "配置或分块计划无效"))
        else:
            checks.append(PreflightCheck(f"{role}.valid", True, "配置及计划可校验"))
    if len(configurations) != 4 or len(bindings) != 3:
        return RevisionPreflight(tuple(checks), bindings)

    source, target = configurations["previous"], configurations["api"]
    checks.append(
        PreflightCheck(
            "previous.admission_paused",
            not source.ingest_policy.accept_new_tasks,
            "旧端配置必须暂停新入库及手动重试；不证明运行实例已停投",
        )
    )
    for role, config in configurations.items():
        infrastructure = config.infrastructure_config
        complete = (
            config.backends.task_store is Backend.POSTGRES
            and config.backends.task_executor is Backend.ARQ
            and config.backends.vector_store is Backend.MILVUS
            and infrastructure.database is not None
            and infrastructure.redis is not None
            and infrastructure.vector_database is not None
        )
        checks.append(
            PreflightCheck(
                f"{role}.persistent", complete, "需要明确的 PG/Redis/Milvus 配置"
            )
        )
    for role, binding in bindings.items():
        checks.append(
            PreflightCheck(
                f"{role}.writer_binding",
                binding is not None and binding == bindings["api"],
                "新端三个角色必须启用 revision writer 且绑定一致",
            )
        )

    old, new = source.infrastructure_config, target.infrastructure_config
    checks.extend(
        (
            PreflightCheck(
                "namespace.postgres",
                old.database is not None
                and new.database is not None
                and old.database.database != new.database.database,
                "新旧 SQL database 名称必须不同",
            ),
            PreflightCheck(
                "namespace.redis",
                old.redis is not None
                and new.redis is not None
                and old.redis.db_index != new.redis.db_index,
                "新旧 Redis DB index 必须不同，固定队列名不能共用 DB",
            ),
            PreflightCheck(
                "namespace.milvus",
                old.vector_database is not None
                and new.vector_database is not None
                and (
                    old.vector_database.database_name
                    != new.vector_database.database_name
                    or old.vector_database.collection_prefix
                    != new.vector_database.collection_prefix
                ),
                "新旧 Milvus database 或 collection prefix 必须不同",
            ),
        )
    )
    return RevisionPreflight(tuple(checks), bindings)
