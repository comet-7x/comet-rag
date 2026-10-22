from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from openai import AsyncOpenAI, OpenAI

from comet_rag.exceptions import CometRAGException
from comet_rag.infrastructure.models.embedding.base import (
    AsyncBaseEmbeddingModel,
    BaseEmbeddingModel,
)
from comet_rag.infrastructure.models.embedding.wire import decode_vector

#: OpenAI 官方 ``/embeddings`` 单请求的输入条数上限。
#:
#: 兼容该协议的自建服务（vLLM、TEI 等）上限各不相同，所以它只是**默认值**，
#: 不是协议保证 —— 指向别的服务时应按实际能力显式传 ``batch_limit``。
DEFAULT_OPENAI_BATCH_LIMIT = 2048


class _OpenAIEmbeddingFormat:
    def _configure(self, model_name: str, batch_limit: int) -> None:
        if batch_limit <= 0:
            raise ValueError(f"batch_limit 必须大于 0，收到 {batch_limit}")
        self._model_name = model_name
        self.batch_limit = batch_limit

    def _request_options(
        self, input_data: str | Sequence[str], **kwargs: Any
    ) -> dict[str, Any]:
        """同步/异步共用请求构造，认证与模型名不能被透传参数覆盖。"""
        return {**kwargs, "input": input_data, "model": self._model_name}

    @staticmethod
    def _vectors(response: Any, *, expected_count: int) -> list[list[float]]:
        """按服务端返回的 index 恢复输入顺序，并把 base64 解回浮点数组。

        OpenAI SDK 只在调用方**没有**显式指定 `encoding_format` 时才自动解码；
        显式传 `"base64"`（经 `**kwargs` 透传）时拿到的是字符串，于是
        `embed_query` 声明 `list[float]`、实际给出 `str`。与 Qwen 适配器同病，
        用同一个 `decode_vector`。
        """
        ordered = sorted(response.data, key=lambda item: item.index)
        indexes = [item.index for item in ordered]
        if indexes != list(range(expected_count)):
            raise ValueError(
                f"Embedding 响应索引 {indexes} 无法与 {expected_count} 条输入对齐"
            )
        return [decode_vector(item.embedding) for item in ordered]


class OpenAIEmbeddingModel(_OpenAIEmbeddingFormat, BaseEmbeddingModel):
    """OpenAI 兼容文本嵌入适配器。"""

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        client: OpenAI | None = None,
        batch_limit: int = DEFAULT_OPENAI_BATCH_LIMIT,
    ) -> None:
        """注入客户端由调用方关闭；未注入时只创建当前执行模式的客户端。"""
        self._configure(model_name, batch_limit)
        self._owns_client = client is None
        self.client = (
            client if client is not None else OpenAI(base_url=base_url, api_key=api_key)
        )

    def _embed(self, text: str, **kwargs: Any) -> list[float]:
        """
        同步生成文本嵌入向量。

        Args:
            text: 需要嵌入的文本内容
            **kwargs: 透传给 embeddings.create()（如 dimensions、encoding_format）

        Returns:
            嵌入向量（浮点数列表）
        """
        try:
            response = self.client.embeddings.create(
                **self._request_options(text, **kwargs)
            )
            return self._vectors(response, expected_count=1)[0]
        except Exception as e:
            raise CometRAGException(
                f"{self.__class__.__name__} | embed | 方法操作发生非预期错误：{str(e)}"
            ) from e

    def _embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        """用 OpenAI 原生批量请求生成文档向量：一次请求，多篇文档。"""
        try:
            response = self.client.embeddings.create(
                **self._request_options(list(documents), **kwargs)
            )
            return self._vectors(response, expected_count=len(documents))
        except Exception as e:
            raise CometRAGException(
                f"{self.__class__.__name__} | embed_batch | 方法操作发生非预期错误：{str(e)}"
            ) from e

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class AsyncOpenAIEmbeddingModel(_OpenAIEmbeddingFormat, AsyncBaseEmbeddingModel):
    """OpenAI 兼容文本嵌入适配器。"""

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        client: AsyncOpenAI | None = None,
        batch_limit: int = DEFAULT_OPENAI_BATCH_LIMIT,
    ) -> None:
        """注入客户端由调用方关闭；未注入时只创建当前执行模式的客户端。"""
        self._configure(model_name, batch_limit)
        self._owns_client = client is None
        self.client = (
            client
            if client is not None
            else AsyncOpenAI(base_url=base_url, api_key=api_key)
        )

    async def _embed(self, text: str, **kwargs: Any) -> list[float]:
        """
        异步生成文本嵌入向量。

        Args:
            text: 需要嵌入的文本内容
            **kwargs: 透传给 embeddings.create()（如 dimensions、encoding_format）

        Returns:
            嵌入向量（浮点数列表）
        """
        try:
            response = await self.client.embeddings.create(
                **self._request_options(text, **kwargs)
            )
            return self._vectors(response, expected_count=1)[0]
        except Exception as e:
            raise CometRAGException(
                f"{self.__class__.__name__} | embed | 方法操作发生非预期错误：{str(e)}"
            ) from e

    async def _embed_batch(
        self, documents: Sequence[str], /, **kwargs: Any
    ) -> list[list[float]]:
        try:
            response = await self.client.embeddings.create(
                **self._request_options(list(documents), **kwargs)
            )
            return self._vectors(response, expected_count=len(documents))
        except Exception as e:
            raise CometRAGException(
                f"{self.__class__.__name__} | embed_batch | 方法操作发生非预期错误：{str(e)}"
            ) from e

    async def close(self) -> None:
        if self._owns_client:
            await self.client.close()
