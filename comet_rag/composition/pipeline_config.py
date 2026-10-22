from __future__ import annotations

from comet_rag.config.schemas import LimitsConfig
from comet_rag.engines.pipelines.types import DocxConfig, PipelineConfig


def resolve_pipeline_config(
    limits: LimitsConfig, overrides: PipelineConfig | None = None
) -> PipelineConfig:
    """以部署限额补齐并校验配置，不修改输入，保留注入模型的身份。"""
    configured_docx = DocxConfig(
        max_archive_members=limits.docx_max_archive_members,
        max_archive_member_bytes=limits.docx_max_archive_member_bytes,
        max_archive_uncompressed_bytes=limits.docx_max_archive_uncompressed_bytes,
        max_archive_compression_ratio=limits.docx_max_archive_compression_ratio,
        max_archive_xml_elements=limits.docx_max_archive_xml_elements,
        max_archive_xml_text_chars=limits.docx_max_archive_xml_text_chars,
    )
    if overrides is None:
        return PipelineConfig(
            docx=configured_docx,
            max_concurrency=limits.pipeline_concurrency,
            embed_batch_size=limits.embed_batch_size,
        )

    # dict(model) 保留原始字段对象；model_dump 会序列化 dataclass vision_model。
    # 嵌套对象可独立赋值，不能用外层 fields_set 判断 DOCX 是否有覆盖。
    docx_values = dict(overrides.docx)
    for name in configured_docx.model_fields_set - overrides.docx.model_fields_set:
        docx_values[name] = getattr(configured_docx, name)
    resolved_docx = type(overrides.docx).model_validate(docx_values)

    # 显式写入的库默认值也是覆盖，不能通过与默认值比较来判断。
    explicit = overrides.model_fields_set
    values = dict(overrides)
    values.update(
        {
            "docx": resolved_docx,
            "max_concurrency": (
                overrides.max_concurrency
                if "max_concurrency" in explicit
                else limits.pipeline_concurrency
            ),
            "embed_batch_size": (
                overrides.embed_batch_size
                if "embed_batch_size" in explicit
                else limits.embed_batch_size
            ),
        }
    )
    # model_copy 和 model_validate(existing_model) 都不会重新校验赋值后的字段。
    return type(overrides).model_validate(values)
