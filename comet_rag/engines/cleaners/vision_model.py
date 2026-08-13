from typing import Protocol


class VisionModel(Protocol):
    def describe(self, base64_data: str, media_type: str, **kwargs) -> str:
        """
        Generate a text description of an image.

        Parameters:
            base64_data (str): Base64-encoded image data.
            media_type (str): The MIME type of the image (e.g., "image/png").

        Returns:
            str: A text description of the image.
        """
        ...

    async def adescribe(self, base64_data: str, media_type: str, **kwargs) -> str:
        """
        Asynchronously generate a text description of an image.

        Parameters:
            base64_data (str): Base64-encoded image data.
            media_type (str): The MIME type of the image (e.g., "image/png").

        Returns:
            str: A text description of the image.
        """
        ...
