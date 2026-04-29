from typing import Any

from pydantic import BaseModel


class BaseParsedContent(BaseModel):
    text: Any
    metadata: dict[str, Any]
