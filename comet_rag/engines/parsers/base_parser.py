from abc import ABC, abstractmethod
from typing import Any

from comet_rag.engines.converters.types import BaseDocument
from comet_rag.engines.parsers.types import BaseParsedContent


class BaseParser[DocumentT: BaseDocument[Any], ParsedContentT: BaseParsedContent](ABC):
    @abstractmethod
    def parse(self, document: DocumentT) -> ParsedContentT: ...

    @abstractmethod
    async def aparse(self, document: DocumentT) -> ParsedContentT: ...
