from __future__ import annotations

from dataclasses import asdict

import pytest
from botocore.exceptions import ClientError

from evals.pdf.s3_diagnostics import header_tamper_rejected, signed_condition
from tests.integration.test_s3_evidence_minio import scope as scope

pytestmark = pytest.mark.integration


async def test_signed_condition_reaches_authentication_and_tampering_is_rejected(
    scope,
) -> None:
    client, _, bucket = scope
    mode = ["normal"]
    observations = []

    def observe(request, **kwargs):
        observations.append(signed_condition(dict(request.headers)))
        if mode[0] == "stripped":
            del request.headers["If-None-Match"]
        elif mode[0] == "altered":
            request.headers["If-None-Match"] = "altered-after-signature"

    client.meta.events.register("before-send.s3.PutObject", observe)
    responses = []
    for name in ("normal", "stripped", "altered"):
        mode[0] = name
        try:
            response = await client.put_object(
                Bucket=bucket,
                Key="diagnostic/" + name,
                Body=b"owned diagnostic",
                IfNoneMatch="*",
            )
        except ClientError as exc:
            responses.append(
                (
                    exc.response["ResponseMetadata"]["HTTPStatusCode"],
                    exc.response["Error"]["Code"],
                )
            )
        else:
            responses.append((response["ResponseMetadata"]["HTTPStatusCode"], ""))
    assert len(observations) == 3
    assert all(
        asdict(fact) == {"sigv4": True, "star": True, "signed": True}
        for fact in observations
    )
    assert header_tamper_rejected(
        observations[0],
        baseline_status=responses[0][0],
        stripped=responses[1],
        altered=responses[2],
    )
