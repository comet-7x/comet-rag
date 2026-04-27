from typing import Any

from pydantic import BaseModel


class BaseDocument(BaseModel):
    elements: Any
    metadata: dict[str, Any]


class ByteDocument(BaseDocument):
    elements: bytes
