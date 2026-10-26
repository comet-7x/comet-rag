from __future__ import annotations

import pytest

from tests.fixtures.minio import credentials, isolated_object


def test_credentials_default_and_explicit_values_are_not_in_repr() -> None:
    defaults = credentials({})
    assert defaults.access_key == defaults.secret_key == "minioadmin"
    value = credentials(
        {
            "COMET_TEST_MINIO_ACCESS_KEY": "test-access",
            "COMET_TEST_MINIO_SECRET_KEY": "test-secret",
        }
    )
    assert value.access_key == "test-access" and value.secret_key == "test-secret"  # noqa: S105 - synthetic fixture
    assert "test-access" not in repr(value) and "test-secret" not in repr(value)


@pytest.mark.parametrize(
    "values",
    [
        {"COMET_TEST_MINIO_ACCESS_KEY": "only-key"},
        {"COMET_TEST_MINIO_SECRET_KEY": "only-secret"},
        {
            "COMET_TEST_MINIO_ACCESS_KEY": "",
            "COMET_TEST_MINIO_SECRET_KEY": "test-secret",
        },
        {
            "COMET_TEST_MINIO_ACCESS_KEY": "test-access",
            "COMET_TEST_MINIO_SECRET_KEY": " ",
        },
    ],
)
def test_partial_or_blank_credentials_cannot_fall_back(values: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="同时配置且非空"):
        credentials(values)


class Client:
    def __init__(self, fault: str) -> None:
        self.fault = fault
        self.calls: list[tuple[str, str, str | None]] = []

    async def create_bucket(self, *, Bucket: str) -> None:
        self.calls.append(("create", Bucket, None))

    async def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.calls.append(("put", Bucket, Key))
        if self.fault == "put":
            raise OSError("PUT response lost")

    async def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.calls.append(("delete-object", Bucket, Key))
        if self.fault == "delete":
            raise OSError("delete response lost")

    async def delete_bucket(self, *, Bucket: str) -> None:
        self.calls.append(("delete-bucket", Bucket, None))


@pytest.mark.parametrize("fault", ["", "put", "consumer", "delete"])
async def test_setup_or_consumer_failure_still_cleans_only_own_object(
    fault: str,
) -> None:
    client = Client(fault)

    async def exercise() -> None:
        async with isolated_object(client, b"payload") as (bucket, key):
            assert (
                bucket.startswith("comet-rag-test-")
                and key == "documents/integration.txt"
            )
            if fault == "consumer":
                raise OSError("consumer failure")

    if fault:
        with pytest.raises(OSError):
            await exercise()
    else:
        await exercise()
    assert [c[0] for c in client.calls] == [
        "create",
        "put",
        "delete-object",
        "delete-bucket",
    ]
    assert len({c[1] for c in client.calls}) == 1
    assert [c[2] for c in client.calls if c[2] is not None] == [
        "documents/integration.txt"
    ] * 2
