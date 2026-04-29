from typing import Any

from docx.document import Document as DocumentObject
from pydantic import BaseModel


class BaseDocument[T](BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    elements: T
    metadata: dict[str, Any]


class ByteDocument(BaseDocument[bytes]):
    pass


class DocxDocument(BaseDocument[DocumentObject]):
    pass
