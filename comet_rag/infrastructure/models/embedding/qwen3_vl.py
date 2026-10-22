from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal, final

from httpx import AsyncClient, Client
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field

from comet_rag.engines.offload import run_sync
from comet_rag.exceptions import CometRAGException
from comet_rag.infrastructure.models.embedding.base import (
    AsyncBaseEmbeddingModel,
    AsyncMultimodalEmbeddingMixin,
    BaseEmbeddingModel,
    MultimodalEmbeddingMixin,
)
from comet_rag.infrastructure.models.embedding.wire import decode_vector
from comet_rag.infrastructure.models.media import (
    DEFAULT_MAX_LOCAL_IMAGE_BYTES,
    ImageReferenceValidator,
    prepare_image_reference,
    prepare_media_resource,
)
from comet_rag.ports import ContentInput, ImageContent, MediaResource, TextContent
from comet_rag.ports.embedding import EmbeddingTask


class Qwen3VLEmbeddingModelSystemPrompt(StrEnum):
    COMMON = "Represent the user's input."  # 通用场景
    RETRIEVAL = "Represent the document for retrieval."  # 文档嵌入场景
    QUERY = "Represent the query for retrieval."  # 查询嵌入场景
    CLASSIFICATION = "Represent the user's input for classification."  # 分类场景


class EmbeddingData(BaseModel):
    """Qwen 旧版请求对象。

    新代码优先使用 ``MediaResource`` 与类型化内容块；保留该类型是为了兼容
    已有调用者以及 tokenize/detokenize 等供应商专属能力。
    """

    text: str | None = Field(default=None, description="需要嵌入的文本内容")
    image_url: str | None = Field(
        default=None,
        description="图片的本地路径、HTTP(S) URL 或 Base64 Data URL",
    )


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
    data: list[EmbeddingParam] = Field(
        ..., min_length=1, description="嵌入向量列表，至少包含当前请求的一个结果"
    )
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


class _QwenEmbeddingFormat:
    """共享配置、请求词汇与媒体准入逻辑；不持有 HTTP 客户端。"""

    def _configure(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        output_dim: int | None = None,
        max_model_len: int | None = None,
        image_url_validator: ImageReferenceValidator | None = None,
        local_image_validator: ImageReferenceValidator | None = None,
        max_local_image_bytes: int = DEFAULT_MAX_LOCAL_IMAGE_BYTES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._api_key = api_key
        self._output_dim = output_dim
        self._max_model_len = max_model_len
        self._image_url_validator = image_url_validator
        self._local_image_validator = local_image_validator
        self._max_local_image_bytes = max_local_image_bytes

    def _get_headers(self) -> dict[str, str]:
        """构造认证请求头；调用方不能通过任意参数覆盖凭据。"""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _task_options(self, task: EmbeddingTask) -> dict[str, Any]:
        """把公共的 query/document 语义映射成 Qwen 指令。"""
        prompt = (
            Qwen3VLEmbeddingModelSystemPrompt.QUERY
            if task is EmbeddingTask.QUERY
            else Qwen3VLEmbeddingModelSystemPrompt.RETRIEVAL
        )
        return {"system_prompt": prompt}

    def _resource_reference(self, resource: MediaResource) -> str:
        return prepare_media_resource(
            resource,
            url_validator=self._image_url_validator,
            local_path_validator=self._local_image_validator,
            max_local_bytes=self._max_local_image_bytes,
        )

    def _normalize_content(self, content: ContentInput) -> EmbeddingData:
        if isinstance(content, str):
            return EmbeddingData(text=content)

        text_parts: list[str] = []
        image: MediaResource | None = None
        for part in content:
            if isinstance(part, TextContent):
                text_parts.append(part.text)
            elif isinstance(part, ImageContent):
                if image is not None:
                    raise ValueError("Qwen3-VL Embedding 单次输入当前只支持一张图片")
                image = part.resource
            else:
                raise TypeError(f"不支持的多模态内容类型：{type(part).__name__}")
        return EmbeddingData(
            text="\n".join(text_parts) or None,
            image_url=None if image is None else self._resource_reference(image),
        )

    def _normalize_input(
        self,
        data: EmbeddingData
        | str
        | MediaResource
        | Sequence[TextContent | ImageContent],
    ) -> EmbeddingData:
        if isinstance(data, EmbeddingData):
            return self._prepare_embedding_data(data)
        if isinstance(data, MediaResource):
            return EmbeddingData(image_url=self._resource_reference(data))
        return self._normalize_content(data)

    def _prepare_embedding_data(self, embedding_data: EmbeddingData) -> EmbeddingData:
        """把本地路径转换为 Data URL，且不修改调用方传入的模型。"""
        if embedding_data.image_url is None:
            return embedding_data
        image_url = prepare_image_reference(
            embedding_data.image_url,
            url_validator=self._image_url_validator,
            local_path_validator=self._local_image_validator,
            max_local_bytes=self._max_local_image_bytes,
        )
        return embedding_data.model_copy(update={"image_url": image_url})

    def _build_embedding_request(
        self,
        embedding_data: EmbeddingData,
        *,
        system_prompt: Qwen3VLEmbeddingModelSystemPrompt | str,
        encoding_format: EncodingFormat,
        continue_final_message: bool,
        add_special_tokens: bool,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        """同步/异步请求共享唯一一份构造逻辑。"""
        messages = self._create_messages_params(
            text=embedding_data.text,
            image_url=embedding_data.image_url,
            continue_final_message=continue_final_message,
            system_prompt=system_prompt,
        )
        return {
            **options,
            "messages": messages,
            "model": self._model_name,
            "encoding_format": encoding_format,
            "continue_final_message": continue_final_message,
            "add_special_tokens": add_special_tokens,
        }

    def _build_tokenize_request(
        self,
        embedding_data: EmbeddingData,
        *,
        continue_final_message: bool,
        return_token_strs: bool,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "model": self._model_name,
            "messages": self._create_messages_params(
                text=embedding_data.text,
                image_url=embedding_data.image_url,
                continue_final_message=continue_final_message,
                system_prompt=Qwen3VLEmbeddingModelSystemPrompt.COMMON,
            ),
            "continue_final_message": continue_final_message,
            "return_token_strs": return_token_strs,
            **options,
        }

    @staticmethod
    def _parse_embedding_response(data: dict[str, Any]) -> list[float]:
        """解析响应，**始终**还原成浮点数组。

        `encoding_format=base64` 是 OpenAI 协议里的传输优化（小一半的报文），
        不是调用方该看到的东西。就地解回来，否则 `embed_query` 声明返回
        `list[float]`、实际给出 `str` —— 契约在说谎，而且只在配了 base64
        的部署上炸。
        """
        return decode_vector(EmbeddingResponse.model_validate(data).data[0].embedding)

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
            image_url (str | None): 图片 URL 或 Base64 Data URL；本地路径会在此前转换
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

    def _validate_output_dim(self, embedding: list[float]) -> int:
        if not isinstance(embedding, list):
            raise ValueError("模型在 encoding_format=float 时返回了非浮点向量")
        actual_dim = len(embedding)

        if self._output_dim is None:
            self._output_dim = actual_dim
        elif self._output_dim != actual_dim:
            raise ValueError(
                f"{self.__class__.__name__} | get_output_dim | 模型输出维度冲突: 配置为 {self._output_dim}, 但模型实际输出为 {actual_dim}"
            )

        return self._output_dim

    def _validate_max_model_len(self, response: TokenizeResponse) -> int:
        actual_len = response.max_model_len

        if self._max_model_len is None:
            self._max_model_len = actual_len
        elif self._max_model_len != actual_len:
            raise ValueError(
                f"{self.__class__.__name__} | get_max_model_len | 模型最大输入长度冲突: 配置为 {self._max_model_len}, 但模型实际返回为 {actual_len}"
            )
        return self._max_model_len


class Qwen3VLEmbeddingModel(
    _QwenEmbeddingFormat, MultimodalEmbeddingMixin, BaseEmbeddingModel
):
    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        output_dim: int | None = None,
        max_model_len: int | None = None,
        client: Client | None = None,
        image_url_validator: ImageReferenceValidator | None = None,
        local_image_validator: ImageReferenceValidator | None = None,
        max_local_image_bytes: int = DEFAULT_MAX_LOCAL_IMAGE_BYTES,
    ) -> None:
        """只创建当前执行模式的客户端；外部注入客户端仍由调用方持有。"""
        self._configure(
            base_url,
            model_name,
            api_key,
            output_dim,
            max_model_len,
            image_url_validator,
            local_image_validator,
            max_local_image_bytes,
        )
        self._owns_client = client is None
        self.client = client if client is not None else Client(timeout=60.0)

    def _embed(
        self,
        embedding_data: (
            EmbeddingData | str | MediaResource | Sequence[TextContent | ImageContent]
        ),
        system_prompt: (
            Qwen3VLEmbeddingModelSystemPrompt | str
        ) = Qwen3VLEmbeddingModelSystemPrompt.COMMON,
        encoding_format: EncodingFormat = EncodingFormat.FLOAT,
        continue_final_message: bool = True,
        add_special_tokens: bool = True,
        **kwargs: Any,
    ) -> list[float]:
        """编码文本或图片；本地图片先转换为 Data URL。"""
        try:
            embedding_data = self._normalize_input(embedding_data)
            payload = self._build_embedding_request(
                embedding_data,
                system_prompt=system_prompt,
                encoding_format=encoding_format,
                continue_final_message=continue_final_message,
                add_special_tokens=add_special_tokens,
                options=kwargs,
            )
            response = self.client.post(
                url=f"{self._base_url}/embeddings",
                headers=self._get_headers(),
                json=payload,
            )
            response.raise_for_status()
            return self._parse_embedding_response(response.json())
        except Exception as e:
            error_msg = (
                f"{self.__class__.__name__} | embed 方法操作发生非预期错误：{str(e)}"
            )
            raise CometRAGException(error_msg) from e

    def _embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """本适配器支持多模态，直接走内部编码路径。

        调未加闸的 `_embed`：闸门已由外壳 `embed_media` 持有，再走一层加闸
        入口会自锁。
        """
        return self._embed(data, **kwargs)

    def embed_image(self, image: MediaResource, /, **kwargs: Any) -> list[float]:
        """生成单张图片的向量。"""
        return self.embed_media(image, **kwargs)

    def embed_content(self, content: ContentInput, /, **kwargs: Any) -> list[float]:
        """生成文本、图片或二者组合内容的向量。"""
        return self.embed_media(content, **kwargs)

    @final
    def tokenize(
        self,
        embedding_data: EmbeddingData,
        continue_final_message: bool = False,
        return_token_strs: bool = False,
        **kwargs: Any,
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
        return self._through_gate_sync(
            lambda: self._tokenize(
                embedding_data,
                continue_final_message=continue_final_message,
                return_token_strs=return_token_strs,
                **kwargs,
            )
        )

    def _tokenize(
        self,
        embedding_data: EmbeddingData,
        continue_final_message: bool = False,
        return_token_strs: bool = False,
        **kwargs: Any,
    ) -> TokenizeResponse:
        """执行一次同步 tokenize 请求；公开入口负责获取闸门名额。"""
        try:
            embedding_data = self._prepare_embedding_data(embedding_data)
            payload = self._build_tokenize_request(
                embedding_data,
                continue_final_message=continue_final_message,
                return_token_strs=return_token_strs,
                options=kwargs,
            )
            response = self.client.post(
                f"{self._base_url.removesuffix('/v1')}/tokenize",
                headers=self._get_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

            return TokenizeResponse(**data)

        except Exception as e:
            error_msg = (
                f"{self.__class__.__name__} | tokenize 方法操作发生非预期错误：{str(e)}"
            )
            raise CometRAGException(error_msg) from e

    @final
    def detokenize(
        self,
        tokens: list[int],
        **kwargs: Any,
    ) -> DetokenizeResponse:
        """同步还原 token；与其他模型请求共用进程级闸门。"""
        return self._through_gate_sync(lambda: self._detokenize(tokens, **kwargs))

    def _detokenize(
        self,
        tokens: list[int],
        **kwargs: Any,
    ) -> DetokenizeResponse:
        """执行一次同步 detokenize 请求。"""
        try:
            response = self.client.post(
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

    def get_output_dim(self) -> int:
        """请求模型并校验输出维度；请求同样受闸门保护。"""
        embedding = self.embed("hello", encoding_format=EncodingFormat.FLOAT)
        return self._validate_output_dim(embedding)

    def get_max_model_len(self) -> int:
        """请求模型并校验最大输入长度。"""
        response = self.tokenize(EmbeddingData(text="hello"))
        return self._validate_max_model_len(response)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class AsyncQwen3VLEmbeddingModel(
    _QwenEmbeddingFormat, AsyncMultimodalEmbeddingMixin, AsyncBaseEmbeddingModel
):
    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        output_dim: int | None = None,
        max_model_len: int | None = None,
        client: AsyncClient | None = None,
        image_url_validator: ImageReferenceValidator | None = None,
        local_image_validator: ImageReferenceValidator | None = None,
        max_local_image_bytes: int = DEFAULT_MAX_LOCAL_IMAGE_BYTES,
    ) -> None:
        """只创建当前执行模式的客户端；外部注入客户端仍由调用方持有。"""
        self._configure(
            base_url,
            model_name,
            api_key,
            output_dim,
            max_model_len,
            image_url_validator,
            local_image_validator,
            max_local_image_bytes,
        )
        self._owns_client = client is None
        self.client = client if client is not None else AsyncClient(timeout=60.0)

    async def _embed(
        self,
        embedding_data: (
            EmbeddingData | str | MediaResource | Sequence[TextContent | ImageContent]
        ),
        system_prompt: (
            Qwen3VLEmbeddingModelSystemPrompt | str
        ) = Qwen3VLEmbeddingModelSystemPrompt.COMMON,
        encoding_format: EncodingFormat = EncodingFormat.FLOAT,
        continue_final_message: bool = True,
        add_special_tokens: bool = True,
        **kwargs: Any,
    ) -> list[float]:
        """异步编码文本或图片；本地读取在线程中执行。"""
        try:
            embedding_data = await run_sync(
                self._normalize_input,
                embedding_data,
            )
            payload = self._build_embedding_request(
                embedding_data,
                system_prompt=system_prompt,
                encoding_format=encoding_format,
                continue_final_message=continue_final_message,
                add_special_tokens=add_special_tokens,
                options=kwargs,
            )
            response = await self.client.post(
                url=f"{self._base_url}/embeddings",
                headers=self._get_headers(),
                json=payload,
            )
            response.raise_for_status()
            return self._parse_embedding_response(response.json())
        except Exception as e:
            error_msg = (
                f"{self.__class__.__name__} | embed 方法操作发生非预期错误：{str(e)}"
            )
            raise CometRAGException(error_msg) from e

    async def _embed_media(
        self, data: MediaResource | ContentInput, /, **kwargs: Any
    ) -> list[float]:
        # 闸门已由 `embed_media` 持有，这里调未加闸的 `_embed`
        return await self._embed(data, **kwargs)

    async def embed_image(self, image: MediaResource, /, **kwargs: Any) -> list[float]:
        """异步生成单张图片的向量。"""
        return await self.embed_media(image, **kwargs)

    async def embed_content(
        self, content: ContentInput, /, **kwargs: Any
    ) -> list[float]:
        """异步生成文本、图片或二者组合内容的向量。"""
        return await self.embed_media(content, **kwargs)

    @final
    async def tokenize(
        self,
        embedding_data: EmbeddingData,
        continue_final_message: bool = False,
        return_token_strs: bool = False,
        **kwargs: Any,
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
        return await self._through_gate(
            lambda: self._tokenize(
                embedding_data,
                continue_final_message=continue_final_message,
                return_token_strs=return_token_strs,
                **kwargs,
            )
        )

    async def _tokenize(
        self,
        embedding_data: EmbeddingData,
        continue_final_message: bool = False,
        return_token_strs: bool = False,
        **kwargs: Any,
    ) -> TokenizeResponse:
        """执行一次异步 tokenize 请求；公开入口负责获取闸门名额。"""
        try:
            embedding_data = await run_sync(
                self._prepare_embedding_data, embedding_data
            )
            payload = self._build_tokenize_request(
                embedding_data,
                continue_final_message=continue_final_message,
                return_token_strs=return_token_strs,
                options=kwargs,
            )
            response = await self.client.post(
                f"{self._base_url.removesuffix('/v1')}/tokenize",
                headers=self._get_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

            return TokenizeResponse(**data)

        except Exception as e:
            error_msg = (
                f"{self.__class__.__name__} | tokenize 方法操作发生非预期错误：{str(e)}"
            )
            raise CometRAGException(error_msg) from e

    @final
    async def detokenize(
        self,
        tokens: list[int],
        **kwargs: Any,
    ) -> DetokenizeResponse:
        """异步还原 token；与其他模型请求共用进程级闸门。"""
        return await self._through_gate(lambda: self._detokenize(tokens, **kwargs))

    async def _detokenize(
        self,
        tokens: list[int],
        **kwargs: Any,
    ) -> DetokenizeResponse:
        """执行一次异步 detokenize 请求。"""
        try:
            response = await self.client.post(
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

    async def get_output_dim(self) -> int:
        """请求模型并校验输出维度；请求同样受闸门保护。"""
        embedding = await self.embed("hello", encoding_format=EncodingFormat.FLOAT)
        return self._validate_output_dim(embedding)

    async def get_max_model_len(self) -> int:
        """请求模型并校验最大输入长度。"""
        response = await self.tokenize(EmbeddingData(text="hello"))
        return self._validate_max_model_len(response)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
