from abc import ABC, abstractmethod


class BaseCleaner(ABC):
    @abstractmethod
    def clean_to_markdown(self, *args, **kwargs) -> str: ...

    @abstractmethod
    async def aclean_to_markdown(self, *args, **kwargs) -> str: ...
