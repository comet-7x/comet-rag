from typing import Any

from openai import AsyncOpenAI, OpenAI
from openai.types.chat import ChatCompletionMessageParam

from comet_rag.exceptions import CometRAGException


class OpenAIVisionModel:
    """OpenAI ``/chat/completions`` 兼容视觉模型适配器。

    该实现以结构化类型满足 ``engines.cleaners.VisionModel`` 协议，无需让
    engines 层反向导入基础设施实现。
    """

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        system_prompt: str = "你是一个图像分析助手，请详细描述图像中的内容。",
        async_client: AsyncOpenAI | None = None,
        sync_client: OpenAI | None = None,
    ) -> None:
        """
        Args:
            base_url: 模型服务地址（vLLM、本地或云端 OpenAI 兼容端点）
            model_name: 模型名称，运行时决定（如 Qwen2.5-VL-72B、gpt-4o）
            api_key: 服务 API Key
            system_prompt: 系统提示，控制描述风格
            async_client: 复用已有异步客户端；为 None 时内部创建
            sync_client: 复用已有同步客户端；为 None 时内部创建

        传入的客户端由调用方持有，本适配器只关闭自己创建的客户端。
        """
        self._model_name = model_name
        self._system_prompt = system_prompt

        self._owns_async_client = async_client is None
        self._owns_sync_client = sync_client is None
        self.async_client = (
            async_client
            if async_client is not None
            else AsyncOpenAI(base_url=base_url, api_key=api_key)
        )
        self.sync_client = (
            sync_client
            if sync_client is not None
            else OpenAI(base_url=base_url, api_key=api_key)
        )

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
            response = self.sync_client.chat.completions.create(
                model=self._model_name,
                messages=self._build_messages(base64_data, media_type),
                **kwargs,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            raise CometRAGException(
                f"{self.__class__.__name__} | describe | 方法操作发生非预期错误：{str(e)}"
            ) from e

    async def adescribe(self, base64_data: str, media_type: str, **kwargs: Any) -> str:
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
            response = await self.async_client.chat.completions.create(
                model=self._model_name,
                messages=self._build_messages(base64_data, media_type),
                **kwargs,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            raise CometRAGException(
                f"{self.__class__.__name__} | adescribe | 方法操作发生非预期错误：{str(e)}"
            ) from e

    async def aclose(self) -> None:
        """仅关闭由当前适配器创建的同步与异步客户端。"""
        if self._owns_async_client:
            await self.async_client.close()
        if self._owns_sync_client:
            self.sync_client.close()
