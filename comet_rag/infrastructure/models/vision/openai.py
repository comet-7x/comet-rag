from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI, OpenAI
from openai.types.chat import ChatCompletionMessageParam

from comet_rag.exceptions import CometRAGException


class _VisionMessages:
    def _configure(self, model_name: str, system_prompt: str) -> None:
        self._model_name = model_name
        self._system_prompt = system_prompt

    def _build_messages(
        self, base64_data: str, media_type: str
    ) -> list[ChatCompletionMessageParam]:
        return [
            {"role": "system", "content": self._system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{base64_data}"},
                    }
                ],
            },
        ]


class OpenAIVisionModel(_VisionMessages):
    """OpenAI 兼容视觉描述适配器。"""

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        system_prompt: str = "你是一个图像分析助手，请详细描述图像中的内容。",
        client: OpenAI | None = None,
    ) -> None:
        """注入客户端由调用方关闭；适配器只拥有自己创建的客户端。"""
        self._configure(model_name, system_prompt)
        self._owns_client = client is None
        self.client = (
            client if client is not None else OpenAI(base_url=base_url, api_key=api_key)
        )

    def describe(self, base64_data: str, media_type: str, **kwargs: Any) -> str:
        """
        同步调用视觉模型描述图像。

        Args:
            base64_data: 图像的 Base64 编码数据
            media_type: 图像 MIME 类型（如 image/png、image/jpeg）
            **kwargs: 透传给 chat.completions.create()（如 max_tokens、temperature）

        Returns:
            模型生成的图像描述文本
        """
        try:
            response = self.client.chat.completions.create(
                model=self._model_name,
                messages=self._build_messages(base64_data, media_type),
                **kwargs,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            raise CometRAGException(
                f"{self.__class__.__name__} | describe | 方法操作发生非预期错误：{str(e)}"
            ) from e

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class AsyncOpenAIVisionModel(_VisionMessages):
    """OpenAI 兼容视觉描述适配器。"""

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        system_prompt: str = "你是一个图像分析助手，请详细描述图像中的内容。",
        client: AsyncOpenAI | None = None,
    ) -> None:
        """注入客户端由调用方关闭；适配器只拥有自己创建的客户端。"""
        self._configure(model_name, system_prompt)
        self._owns_client = client is None
        self.client = (
            client
            if client is not None
            else AsyncOpenAI(base_url=base_url, api_key=api_key)
        )

    async def describe(self, base64_data: str, media_type: str, **kwargs: Any) -> str:
        """
        异步调用视觉模型描述图像。

        Args:
            base64_data: 图像的 Base64 编码数据
            media_type: 图像 MIME 类型（如 image/png、image/jpeg）
            **kwargs: 透传给 chat.completions.create()（如 max_tokens、temperature）

        Returns:
            模型生成的图像描述文本
        """
        try:
            response = await self.client.chat.completions.create(
                model=self._model_name,
                messages=self._build_messages(base64_data, media_type),
                **kwargs,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            raise CometRAGException(
                f"{self.__class__.__name__} | describe | 方法操作发生非预期错误：{str(e)}"
            ) from e

    async def close(self) -> None:
        if self._owns_client:
            await self.client.close()
