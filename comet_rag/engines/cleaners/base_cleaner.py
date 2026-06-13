from abc import abstractmethod


class BaseCleaner:
    @abstractmethod
    def clean_to_string(self, *args, **kwargs) -> str: ...

    @abstractmethod
    async def aclean_to_string(self, *args, **kwargs) -> str: ...
