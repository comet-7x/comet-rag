from enum import StrEnum
from typing import Literal

from httpx import AsyncClient, Client
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field

from comet_rag.exceptions import CometRAGException

from .base import BaseEmbeddingModel


class Qwen3VLEmbeddingModelSystemPrompt(StrEnum):
    COMMON = "Represent the user's input."  # 通用场景
    RETRIEVAL = "Represent the document for retrieval."  # 文档嵌入场景
    QUERY = "Represent the query for retrieval."  # 查询嵌入场景
    CLASSIFICATION = "Represent the user's input for classification."  # 分类场景


class EmbeddingData(BaseModel):
    text: str | None = Field(default=None, description="需要嵌入的文本内容")
    image_url: str | None = Field(
        default=None,
        description="需要嵌入的图片 URL 或图片 Base64 编码数据（如 data:image/jpeg;base64,...）",
    )


def _normalize_embedding_data(data: EmbeddingData | str) -> EmbeddingData:
    """统一基础 embedding 契约与多模态适配器的输入。

    services 层按 ``BaseEmbeddingModel`` 的文本契约传 ``str``；Qwen 适配器还
    支持图片，所以内部使用 ``EmbeddingData``。文本是后者的一个合法子集，
    在适配器边界提升即可，不能要求每个通用调用方认识 Qwen 私有类型。
    """
    return EmbeddingData(text=data) if isinstance(data, str) else data


class EncodingFormat(StrEnum):
    BASE64 = "base64"
    FLOAT = "float"


class EmbeddingParam(BaseModel):
    index: int = Field(..., description="该向量在原始请求列表中的索引位置")
    object: Literal["embedding"] = Field(..., description="对象类型，始终为 embedding")
    embedding: list[float] | str = Field(
        ..., description="生成的嵌入向量。根据请求格式返回浮点数列表或 Base64 字符串"
    )


class Usage(BaseModel):
    prompt_tokens: int = Field(
        ..., description="输入内容（文本及图片）所消耗的 Token 总数"
    )
    completion_tokens: int = Field(
        default=0, description="生成内容消耗的 Token 数量（嵌入任务通常为 0）"
    )
    total_tokens: int = Field(
        ..., description="本次任务消耗的总 Token 数量（prompt + completion）"
    )

    prompt_tokens_details: dict | None = Field(
        default=None,
        description="输入 Token 的细分统计，例如包含的图像 Token (image_tokens) 等",
    )


class EmbeddingResponse(BaseModel):
    id: str = Field(..., description="本次请求的唯一标识符 ID")
    object: str = Field(..., description="返回对象类型，通常为 'list'")
    created: int = Field(..., description="嵌入向量任务创建时间戳（Unix 时间戳）")
    model: str = Field(..., description="使用的模型名称")
    data: list[EmbeddingParam] = Field(..., description="嵌入向量列表")
    usage: Usage = Field(..., description="本次请求的 Token 消耗统计")


class TokenizeResponse(BaseModel):
    count: int = Field(..., description="文本转换后的 Token 总数")
    max_model_len: int = Field(..., description="模型允许的最大输入序列长度")
    tokens: list[int] = Field(..., description="Token ID 序列（整数列表）")
    token_strs: list[str] | None = Field(
        default=None, description="Token 对应的字符串片段列表"
    )


class DetokenizeResponse(BaseModel):
    prompt: str = Field(..., description="从 Token ID 序列还原出的原始文本内容")


class Qwen3VLEmbeddingModel(BaseEmbeddingModel):
    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        output_dim: int | None = None,
        max_model_len: int | None = None,
        async_client: AsyncClient | None = None,
        sync_client: Client | None = None,
    ) -> None:
        """
        Qwen3VL 嵌入模型

        Args:
            base_url (str): 模型服务地址
            model_name (str): 模型名称
            api_key (str): 模型服务 api_key
            output_dim (int | None): 嵌入向量的维度，默认为 `None`
            max_model_len (int | None): 模型允许的最大输入序列长度，默认为 `None`
            async_client (AsyncClient | None): 异步请求连接，默认为 `None`
            sync_client (Client | None): 同步请求连接，默认为 `None`
        """
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._api_key = api_key
        self._output_dim = output_dim
        self._max_model_len = max_model_len

        self._owns_async_client = async_client is None
        self._owns_sync_client = sync_client is None
        self.async_client = (
            async_client if async_client is not None else AsyncClient(timeout=60.0)
        )
        self.sync_client = (
            sync_client if sync_client is not None else Client(timeout=60.0)
        )

    def _get_headers(self, **kwargs) -> dict:
        """
        获取请求头

        Returns:
            dict: 请求头
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **kwargs,
        }
        return headers

    def _create_messages_params(
        self,
        text: str | None = None,
        image_url: str | None = None,
        continue_final_message: bool = True,
        system_prompt: (
            Qwen3VLEmbeddingModelSystemPrompt | str
        ) = Qwen3VLEmbeddingModelSystemPrompt.COMMON,
    ) -> list[ChatCompletionMessageParam]:
        """
        创建消息参数

        Args:
            text (str | None): 文本内容
            image_url (str | None): 图片 URL 或者图片 base64 编码
            system_prompt (Qwen3VLEmbeddingModelSystemPrompt): 系统提示，默认为 `Qwen3VLEmbeddingModelSystemPrompt.COMMON`

        Returns:
            list[ChatCompletionMessageParam]: 消息参数列表
        """
        system_message: ChatCompletionMessageParam = {
            "role": "system",
            "content": [
                {"type": "text", "text": system_prompt},
            ],
        }
        messages: list[ChatCompletionMessageParam] = [system_message]

        user_content = []
        if not text and not image_url:
            raise ValueError("text or image_url must be provided")
        if image_url:
            user_content.append({"type": "image_url", "image_url": {"url": image_url}})
        user_content.append({"type": "text", "text": text if text else ""})

        user_message: ChatCompletionMessageParam = {
            "role": "user",
            "content": user_content,
        }
        messages.append(user_message)

        if continue_final_message:
            assistant_message: ChatCompletionMessageParam = {
                "role": "assistant",
                "content": [{"type": "text", "text": ""}],
            }
            messages.append(assistant_message)

        return messages

    def embed(
        self,
        embedding_data: EmbeddingData | str,
        system_prompt: (
            Qwen3VLEmbeddingModelSystemPrompt | str
        ) = Qwen3VLEmbeddingModelSystemPrompt.COMMON,
        encoding_format: EncodingFormat = EncodingFormat.FLOAT,
        continue_final_message: bool = True,
        add_special_tokens: bool = True,
        **kwargs,
    ) -> list[float] | str:
        """
        编码文本、图片（图片的 base64 编码或者图片 URL） 对应的向量

        Args:
            embedding_data (EmbeddingData): 嵌入数据
            system_prompt (Qwen3VLEmbeddingModelSystemPrompt): 系统提示，默认为 `Qwen3VLEmbeddingModelSystemPrompt.COMMON`
            encoding_format (EncodingFormat): 编码格式，默认为 `EncodingFormat.FLOAT`：
                - `"base64"`：返回 base64 编码的向量表示
                - `"float"`：返回浮点数表示的向量
            continue_final_message (bool): 是否继续最后一条消息，默认为 `True`
            add_special_tokens (bool): 是否添加特殊分隔标记，默认为 `True`

        Returns:
            list[float]: 向量表示
        """
        try:
            embedding_data = _normalize_embedding_data(embedding_data)
            messages = self._create_messages_params(
                text=embedding_data.text,
                image_url=embedding_data.image_url,
                continue_final_message=continue_final_message,
                system_prompt=system_prompt,
            )
            response = self.sync_client.post(
                url=f"{self._base_url}/embeddings",
                headers=self._get_headers(),
                json={
                    "messages": messages,
                    "model": self._model_name,
                    "encoding_format": encoding_format,
                    "continue_final_message": continue_final_message,
                    "add_special_tokens": add_special_tokens,
                    **kwargs,
                },
            )
            response.raise_for_status()
            embedding_response = EmbeddingResponse(**response.json())

            return embedding_response.data[0].embedding
        except Exception as e:
            error_msg = (
                f"{self.__class__.__name__} | embed 方法操作发生非预期错误：{str(e)}"
            )
            raise CometRAGException(error_msg) from e

    async def _aembed(
        self,
        embedding_data: EmbeddingData | str,
        system_prompt: (
            Qwen3VLEmbeddingModelSystemPrompt | str
        ) = Qwen3VLEmbeddingModelSystemPrompt.COMMON,
        encoding_format: EncodingFormat = EncodingFormat.FLOAT,
        continue_final_message: bool = True,
        add_special_tokens: bool = True,
        **kwargs,
    ) -> list[float] | str:
        """
        异步编码文本、图片（图片的 base64 编码或者图片 URL） 对应的向量

        Args:
            embedding_data (EmbeddingData): 嵌入数据
            system_prompt (Qwen3VLEmbeddingModelSystemPrompt): 系统提示，默认为 `Qwen3VLEmbeddingModelSystemPrompt.COMMON`
            encoding_format (EncodingFormat): 编码格式，默认为 `EncodingFormat.FLOAT`：
                - `"base64"`：返回 base64 编码的向量表示
                - `"float"`：返回浮点数表示的向量
            continue_final_message (bool): 是否继续最后一条消息，默认为 `True`
            add_special_tokens (bool): 是否添加特殊分隔标记，默认为 `True`

        Returns:
            list[float]: 向量表示
        """
        try:
            embedding_data = _normalize_embedding_data(embedding_data)
            messages = self._create_messages_params(
                text=embedding_data.text,
                image_url=embedding_data.image_url,
                continue_final_message=continue_final_message,
                system_prompt=system_prompt,
            )
            response = await self.async_client.post(
                url=f"{self._base_url}/embeddings",
                headers=self._get_headers(),
                json={
                    "messages": messages,
                    "model": self._model_name,
                    "encoding_format": encoding_format,
                    "continue_final_message": continue_final_message,
                    "add_special_tokens": add_special_tokens,
                    **kwargs,
                },
            )
            response.raise_for_status()
            embedding_response = EmbeddingResponse(**response.json())

            return embedding_response.data[0].embedding
        except Exception as e:
            error_msg = (
                f"{self.__class__.__name__} | aembed 方法操作发生非预期错误：{str(e)}"
            )
            raise CometRAGException(error_msg) from e

    def tokenize(
        self,
        embedding_data: EmbeddingData,
        continue_final_message: bool = False,
        return_token_strs: bool = False,
        **kwargs,
    ) -> TokenizeResponse:
        """
        编码 tokens 对应的文本

        Args:
            embedding_data (EmbeddingData): 嵌入数据
            continue_final_message (bool): 是否继续最后一条消息，默认为 `False`
            return_token_strs (bool): 是否返回 token 字符串，默认为 `False`

        Returns:
            TokenizeResponse: tokenize 响应
        """
        try:
            messages = self._create_messages_params(
                text=embedding_data.text,
                image_url=embedding_data.image_url,
                continue_final_message=continue_final_message,
                system_prompt=Qwen3VLEmbeddingModelSystemPrompt.COMMON,
            )
            response = self.sync_client.post(
                f"{self._base_url.removesuffix('/v1')}/tokenize",
                headers=self._get_headers(),
                json={
                    "model": self._model_name,
                    "messages": messages,
                    "continue_final_message": continue_final_message,
                    "return_token_strs": return_token_strs,
                    **kwargs,
                },
            )
            response.raise_for_status()
            data = response.json()

            return TokenizeResponse(**data)

        except Exception as e:
            error_msg = (
                f"{self.__class__.__name__} | tokenize 方法操作发生非预期错误：{str(e)}"
            )
            raise CometRAGException(error_msg) from e

    async def atokenize(
        self,
        embedding_data: EmbeddingData,
        continue_final_message: bool = False,
        return_token_strs: bool = False,
        **kwargs,
    ) -> TokenizeResponse:
        """
        异步编码 tokens 对应的文本

        Args:
            embedding_data (EmbeddingData): 嵌入数据
            continue_final_message (bool): 是否继续最后一条消息，默认为 `False`
            return_token_strs (bool): 是否返回 token 字符串，默认为 `False`

        Returns:
            TokenizeResponse: tokenize 响应
        """
        try:
            messages = self._create_messages_params(
                text=embedding_data.text,
                image_url=embedding_data.image_url,
                continue_final_message=continue_final_message,
                system_prompt=Qwen3VLEmbeddingModelSystemPrompt.COMMON,
            )
            response = await self.async_client.post(
                f"{self._base_url.removesuffix('/v1')}/tokenize",
                headers=self._get_headers(),
                json={
                    "model": self._model_name,
                    "messages": messages,
                    "continue_final_message": continue_final_message,
                    "return_token_strs": return_token_strs,
                    **kwargs,
                },
            )
            response.raise_for_status()
            data = response.json()

            return TokenizeResponse(**data)

        except Exception as e:
            error_msg = f"{self.__class__.__name__} | atokenize 方法操作发生非预期错误：{str(e)}"
            raise CometRAGException(error_msg) from e

    def detokenize(
        self,
        tokens: list[int],
        **kwargs,
    ) -> DetokenizeResponse:
        try:
            response = self.sync_client.post(
                f"{self._base_url.removesuffix('/v1')}/detokenize",
                headers=self._get_headers(),
                json={
                    "model": self._model_name,
                    "tokens": tokens,
                    **kwargs,
                },
            )
            response.raise_for_status()
            data = response.json()

            return DetokenizeResponse(**data)

        except Exception as e:
            error_msg = f"{self.__class__.__name__} | detokenize 方法操作发生非预期错误：{str(e)}"
            raise CometRAGException(error_msg) from e

    async def adetokenize(
        self,
        tokens: list[int],
        **kwargs,
    ) -> DetokenizeResponse:
        try:
            response = await self.async_client.post(
                f"{self._base_url.removesuffix('/v1')}/detokenize",
                headers=self._get_headers(),
                json={
                    "model": self._model_name,
                    "tokens": tokens,
                    **kwargs,
                },
            )
            response.raise_for_status()
            data = response.json()

            return DetokenizeResponse(**data)

        except Exception as e:
            error_msg = f"{self.__class__.__name__} | adetokenize 方法操作发生非预期错误：{str(e)}"
            raise CometRAGException(error_msg) from e

    def get_output_dim(self) -> int:
        """获取并校验输出维度"""
        actual_dim = len(
            self.embed(
                EmbeddingData(text="hello"), encoding_format=EncodingFormat.FLOAT
            )
        )

        if self._output_dim is None:
            self._output_dim = actual_dim
        elif self._output_dim != actual_dim:
            raise ValueError(
                f"{self.__class__.__name__} | get_output_dim | 模型输出维度冲突: 配置为 {self._output_dim}, 但模型实际输出为 {actual_dim}"
            )

        return self._output_dim

    def get_max_model_len(self) -> int:
        response = self.tokenize(EmbeddingData(text="hello"))
        actual_len = response.max_model_len

        if self._max_model_len is None:
            self._max_model_len = actual_len
        elif self._max_model_len != actual_len:
            raise ValueError(
                f"{self.__class__.__name__} | get_max_model_len | 模型最大输出长度冲突: 配置为 {self._max_model_len}, 但模型实际输出为 {actual_len}"
            )
        return self._max_model_len

    async def close_client(self) -> None:
        """
        关闭客户端
        """
        if self._owns_async_client:
            await self.async_client.aclose()
        if self._owns_sync_client:
            self.sync_client.close()
