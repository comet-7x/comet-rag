from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import Field, ValidationError

from comet_rag.composition.pipeline_config import resolve_pipeline_config
from comet_rag.config.schemas import LimitsConfig
from comet_rag.engines.pipelines.types import DocxConfig, PipelineConfig


@pytest.fixture
def limits() -> LimitsConfig:
    return LimitsConfig(
        pipeline_concurrency=3,
        embed_batch_size=7,
        docx_max_archive_members=17,
        docx_max_archive_member_bytes=1024,
        docx_max_archive_uncompressed_bytes=4096,
        docx_max_archive_compression_ratio=12.5,
        docx_max_archive_xml_elements=19,
        docx_max_archive_xml_text_chars=256,
    )


@pytest.mark.parametrize(
    "overrides", [None, PipelineConfig(), PipelineConfig(docx=DocxConfig())]
)
def test_deployment_limits_fill_unspecified_fields(
    limits: LimitsConfig, overrides: PipelineConfig | None
) -> None:
    resolved = resolve_pipeline_config(limits, overrides)

    assert resolved.max_concurrency == 3
    assert resolved.embed_batch_size == 7
    assert resolved.docx.max_archive_members == 17
    assert resolved.docx.max_archive_member_bytes == 1024
    assert resolved.docx.max_archive_uncompressed_bytes == 4096
    assert resolved.docx.max_archive_compression_ratio == 12.5
    assert resolved.docx.max_archive_xml_elements == 19
    assert resolved.docx.max_archive_xml_text_chars == 256


def test_explicit_library_defaults_override_deployment_limits(
    limits: LimitsConfig,
) -> None:
    overrides = PipelineConfig(
        max_concurrency=8,
        embed_batch_size=32,
        docx=DocxConfig(max_archive_members=10_000),
    )

    resolved = resolve_pipeline_config(limits, overrides)

    assert resolved.max_concurrency == 8
    assert resolved.embed_batch_size == 32
    assert resolved.docx.max_archive_members == 10_000
    assert resolved.docx.max_archive_xml_elements == 19


def test_unrelated_options_keep_deployment_limits(limits: LimitsConfig) -> None:
    overrides = PipelineConfig(chunk_size=321, chunk_overlap=17, embed=True)

    resolved = resolve_pipeline_config(limits, overrides)

    assert resolved.chunk_size == 321
    assert resolved.chunk_overlap == 17
    assert resolved.embed is True
    assert resolved.max_concurrency == 3
    assert resolved.embed_batch_size == 7
    assert resolved.docx.max_archive_members == 17


def test_nested_assignment_is_an_override_without_setting_outer_field(
    limits: LimitsConfig,
) -> None:
    overrides = PipelineConfig()
    overrides.docx.max_archive_members = 23
    assert "docx" not in overrides.model_fields_set

    resolved = resolve_pipeline_config(limits, overrides)

    assert resolved.docx.max_archive_members == 23
    assert resolved.docx.max_archive_xml_elements == 19


def test_resolution_preserves_inputs_and_injected_model(limits: LimitsConfig) -> None:
    @dataclass(slots=True)
    class VisionModel:
        response: str = "image"

        def describe(self, base64_data: str, media_type: str) -> str:
            return self.response

    model = VisionModel()
    docx = DocxConfig(vision_model=model, include_images=False, heading_numbers=True)
    overrides = PipelineConfig(docx=docx, chunk_size=321)
    original_values = overrides.model_dump()
    original_limits = limits.model_dump()

    resolved = resolve_pipeline_config(limits, overrides)

    assert resolved is not overrides
    assert resolved.docx is not docx
    assert resolved.docx.vision_model is model
    assert resolved.docx.include_images is False
    assert resolved.docx.heading_numbers is True
    assert overrides.docx is docx
    assert overrides.docx.vision_model is model
    assert overrides.model_dump() == original_values
    assert overrides.model_fields_set == {"docx", "chunk_size"}
    assert docx.model_fields_set == {
        "vision_model",
        "include_images",
        "heading_numbers",
    }
    assert limits.model_dump() == original_limits


def test_invalid_docx_override_is_still_validated(limits: LimitsConfig) -> None:
    overrides = PipelineConfig()
    overrides.docx.max_archive_members = 0

    with pytest.raises(ValidationError, match="max_archive_members"):
        resolve_pipeline_config(limits, overrides)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chunk_size", 0),
        ("chunk_overlap", -1),
        ("max_concurrency", 0),
        ("embed_batch_size", 0),
    ],
)
def test_invalid_top_level_assignment_is_rejected(
    limits: LimitsConfig, field: str, value: int
) -> None:
    overrides = PipelineConfig()
    setattr(overrides, field, value)

    with pytest.raises(ValidationError, match=field):
        resolve_pipeline_config(limits, overrides)


def test_resolution_preserves_and_validates_subclass_fields(
    limits: LimitsConfig,
) -> None:
    class CustomPipelineConfig(PipelineConfig):
        retry_limit: int = Field(default=2, gt=0)

    overrides = CustomPipelineConfig(retry_limit=5)

    resolved = resolve_pipeline_config(limits, overrides)

    assert isinstance(resolved, CustomPipelineConfig)
    assert resolved.retry_limit == 5
    assert resolved.max_concurrency == 3
    assert overrides.model_fields_set == {"retry_limit"}

    overrides.retry_limit = 0
    with pytest.raises(ValidationError, match="retry_limit"):
        resolve_pipeline_config(limits, overrides)
