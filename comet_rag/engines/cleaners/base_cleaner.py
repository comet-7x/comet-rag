from abc import ABC, abstractmethod


class BaseCleaner(ABC):
    @abstractmethod
    def clean_to_markdown(self, *args, **kwargs) -> str: ...

    """
    Convert content to Markdown format.

    Returns:
        str: Markdown-formatted string.
    """

    @abstractmethod
    async def aclean_to_markdown(self, *args, **kwargs) -> str: ...

    """
    Convert content to Markdown.

    Returns:
        str: The content converted to Markdown.
    """
