from __future__ import annotations

import pytest

from comet_rag.infrastructure.extractors._mineru_v1_protocol import (
    CaptureError,
    check_health,
    check_tiers,
)


def test_health_and_tiers_accept_fixed_supported_protocol():
    check_health(
        {
            "status": "ok",
            "version": "4.0.12",
            "features": {"output_formats": ["structured_content"]},
        }
    )
    check_tiers({"object": "list", "data": [{"id": "advanced"}]})


@pytest.mark.parametrize(
    "value",
    [
        {},
        {
            "status": "ok",
            "version": "wrong",
            "features": {"output_formats": ["structured_content"]},
        },
        {"status": "ok", "version": "4.0.12", "features": []},
        {
            "status": "ok",
            "version": "4.0.12",
            "features": {"output_formats": "structured_content"},
        },
        {
            "status": "ok",
            "version": "4.0.12",
            "features": {"output_formats": ["markdown"]},
        },
    ],
)
def test_health_rejects_malformed_or_incompatible_server(value):
    with pytest.raises(CaptureError):
        check_health(value)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"object": "list", "data": "advanced"},
        {"object": "list", "data": ["advanced"]},
        {"object": "list", "data": [{"id": "standard"}]},
        {"object": "wrong", "data": [{"id": "advanced"}]},
    ],
)
def test_tiers_rejects_malformed_or_missing_advanced_tier(value):
    with pytest.raises(CaptureError):
        check_tiers(value)
