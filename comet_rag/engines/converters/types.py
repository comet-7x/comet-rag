from typing import Any

from pydantic import BaseModel


class BaseDocument[T](BaseModel):
    elements: T
    metadata: dict[str, Any]


class ByteDocument(BaseDocument[bytes]):
    pass
