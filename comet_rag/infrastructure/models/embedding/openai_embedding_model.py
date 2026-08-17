from typing import Any

from openai import AsyncOpenAI, OpenAI

from comet_rag.exceptions import CometRAGException

from .base import BaseEmbeddingModel


class OpenAIEmbeddingModel(BaseEmbeddingModel):
    """OpenAI ``/embeddings`` 兼容文本嵌入适配器。"""

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        async_client: AsyncOpenAI | None = None,
        sync_client: OpenAI | None = None,
    ) -> None:
        """
        Args:
            base_url: 模型服务地址
            model_name: 模型名称（如 text-embedding-3-small）
            api_key: 服务 API Key
            async_client: 复用已有异步客户端；为 None 时内部创建
            sync_client: 复用已有同步客户端；为 None 时内部创建

        传入的客户端由调用方持有，本适配器只关闭自己创建的客户端。
        """
        self._model_name = model_name

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

    def embed(self, text: str, **kwargs: Any) -> list[float]:
        """
        同步生成文本嵌入向量。

        Args:
            text: 需要嵌入的文本内容
            **kwargs: 透传给 embeddings.create()（如 dimensions、encoding_format）

        Returns:
            嵌入向量（浮点数列表）
        """
        try:
            response = self.sync_client.embeddings.create(
                input=text,
                model=self._model_name,
                **kwargs,
            )
            return response.data[0].embedding
        except Exception as e:
            raise CometRAGException(
                f"{self.__class__.__name__} | embed | 方法操作发生非预期错误：{str(e)}"
            ) from e

    async def _aembed(self, text: str, **kwargs: Any) -> list[float]:
        """
        异步生成文本嵌入向量。

        Args:
            text: 需要嵌入的文本内容
            **kwargs: 透传给 embeddings.create()（如 dimensions、encoding_format）

        Returns:
            嵌入向量（浮点数列表）
        """
        try:
            response = await self.async_client.embeddings.create(
                input=text,
                model=self._model_name,
                **kwargs,
            )
            return response.data[0].embedding
        except Exception as e:
            raise CometRAGException(
                f"{self.__class__.__name__} | aembed | 方法操作发生非预期错误：{str(e)}"
            ) from e

    async def aclose(self) -> None:
        """仅关闭由当前适配器创建的同步与异步客户端。"""
        if self._owns_async_client:
            await self.async_client.close()
        if self._owns_sync_client:
            self.sync_client.close()
