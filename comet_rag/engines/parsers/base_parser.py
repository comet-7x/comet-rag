from abc import ABC, abstractmethod

from comet_rag.engines.converters.types import BaseDocument
from comet_rag.engines.parsers.types import BaseParsedContent


class BaseParser(ABC):
    @abstractmethod
    def parse(self, document: BaseDocument) -> BaseParsedContent: ...

    @abstractmethod
    async def aparse(self, document: BaseDocument) -> BaseParsedContent: ...
