from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from evals.pdf.s3_diagnostics import (
    MinioBuild,
    SignedCondition,
    header_tamper_rejected,
    server_builds,
    signed_condition,
)

VERSION = "2023-03-20T20:16:18Z"
COMMIT = "05444a0f6af8389b9bb85280fc31337c556d4300"


def record():
    return {
        "status": "success",
        "info": {
            "servers": [
                {
                    "version": VERSION,
                    "commitID": COMMIT,
                    "endpoint": "private-host",
                    "env": "secret-value",
                }
            ],
            "services": {"private": "secret-value"},
        },
    }


def test_native_admin_projection_retains_only_builds_and_preserves_mixed_deployment() -> (
    None
):
    value = record()
    value["info"]["servers"] *= 2
    value["info"]["servers"].append(
        {"version": "2025-09-07T16:13:09Z", "commitID": "b" * 40}
    )
    result = server_builds(json.dumps(value).encode())
    assert result == (
        MinioBuild(VERSION, COMMIT),
        MinioBuild("2025-09-07T16:13:09Z", "b" * 40),
    )
    summary = repr(result)
    assert "private-host" not in summary and "secret-value" not in summary


@pytest.mark.parametrize(
    "fault",
    [
        "failed",
        "empty",
        "nodes_type",
        "too_many",
        "node_type",
        "version_bool",
        "version_date",
        "version_private",
        "commit_bool",
        "commit_shape",
        "duplicate_json",
        "oversized",
    ],
)
def test_admin_diagnostics_reject_invalid_or_unbounded_records(fault: str) -> None:
    value = record()
    if fault == "failed":
        value["status"] = "error"
    elif fault == "empty":
        value["info"]["servers"] = []
    elif fault == "nodes_type":
        value["info"]["servers"] = {}
    elif fault == "too_many":
        value["info"]["servers"] *= 129
    elif fault == "node_type":
        value["info"]["servers"] = [None]
    elif fault.startswith("version_"):
        value["info"]["servers"][0]["version"] = {
            "version_bool": True,
            "version_date": "2023-02-30T20:16:18Z",
            "version_private": "https://secret-value",
        }[fault]
    elif fault.startswith("commit_"):
        value["info"]["servers"][0]["commitID"] = (
            True if fault == "commit_bool" else "secret-value"
        )
    raw = json.dumps(value).encode()
    if fault == "duplicate_json":
        raw = b'{"status":"success","status":"error"}'
    elif fault == "oversized":
        raw = b" " * (512 * 1024 + 1)
    with pytest.raises(ValueError, match="512KiB" if fault == "oversized" else None):
        server_builds(raw)


def headers():
    request = AWSRequest(
        method="PUT",
        url="https://s3.invalid/bucket/owned",
        data=b"body",
        headers={"If-None-Match": "*"},
    )
    SigV4Auth(
        Credentials("example-access", "example-secret"), "s3", "us-east-1"
    ).add_auth(request)
    return dict(request.headers)


def test_upstream_sigv4_signs_condition_without_exposing_credentials() -> None:
    result = signed_condition(headers())
    assert result == SignedCondition(True, True, True)
    assert set(asdict(result)) == {"sigv4", "star", "signed"}
    assert "example-access" not in repr(result) and "example-secret" not in repr(result)
    assert header_tamper_rejected(
        result,
        baseline_status=404,
        stripped=(400, "AccessDenied"),
        altered=(403, "SignatureDoesNotMatch"),
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing_header",
        "changed_header",
        "unsigned",
        "other_auth",
        "backend_error",
        "tamper_accepted",
        "tamper_generic_denial",
    ],
)
def test_incomplete_evidence_cannot_exclude_header_loss(fault: str) -> None:
    original = headers()
    baseline, stripped, altered = (
        404,
        (400, "AccessDenied"),
        (403, "SignatureDoesNotMatch"),
    )
    if fault == "missing_header":
        del original["If-None-Match"]
    elif fault == "changed_header":
        original["If-None-Match"] = '"etag"'
    elif fault == "unsigned":
        original["Authorization"] = original["Authorization"].replace(
            "if-none-match;", ""
        )
    elif fault == "other_auth":
        original["Authorization"] = "Bearer example-secret"
    elif fault == "backend_error":
        baseline = 503
    elif fault == "tamper_accepted":
        stripped = (200, "")
    elif fault == "tamper_generic_denial":
        altered = (403, "AccessDenied")
    assert not header_tamper_rejected(
        signed_condition(original),
        baseline_status=baseline,
        stripped=stripped,
        altered=altered,
    )


@pytest.mark.parametrize(
    "fault",
    ["duplicate", "duplicate_name", "invalid_name", "encoding", "duplicate_case"],
)
def test_ambiguous_signature_diagnostic_is_rejected_without_secret_echo(
    fault: str,
) -> None:
    original = headers()
    if fault == "duplicate":
        original["Authorization"] += ", SignedHeaders=host"
    elif fault == "duplicate_name":
        original["Authorization"] = original["Authorization"].replace(
            "SignedHeaders=", "SignedHeaders=host;"
        )
    elif fault == "invalid_name":
        original["Authorization"] = original["Authorization"].replace(
            "SignedHeaders=", "SignedHeaders=bad:name;"
        )
    elif fault == "duplicate_case":
        original["authorization"] = original["Authorization"]
    else:
        original["Authorization"] = b"\xffsecret-value"
    with pytest.raises(ValueError) as error:
        signed_condition(original)
    assert "secret-value" not in str(error.value) and "example-access" not in str(
        error.value
    )
