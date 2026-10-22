from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal

from httpx import AsyncClient, Client
from pydantic import BaseModel, Field

from comet_rag.engines.offload import run_sync
from comet_rag.exceptions import CometRAGException
from comet_rag.infrastructure.models.media import (
    DEFAULT_MAX_LOCAL_IMAGE_BYTES,
    ImageReferenceValidator,
    prepare_image_reference,
    prepare_media_resource,
)
from comet_rag.infrastructure.models.reranker.base import (
    AsyncBaseReranker,
    BaseReranker,
)
from comet_rag.ports import ContentInput, ImageContent, TextContent


class ImageDetail(StrEnum):
    AUTO = "auto"
    LOW = "low"
    HIGH = "high"


class ImageUrlParam(BaseModel):
    url: str = Field(..., description="图片URL")
    detail: ImageDetail = Field(default=ImageDetail.AUTO, description="图片解析精度")


class ImageEmbedsParam(BaseModel):
    embeds: str = Field(..., description="图片的 base64 编码字符串")


class ChatCompletionContentPartImageParam(BaseModel):
    image_url: ImageUrlParam = Field(..., description="图片相关的参数")
    type: Literal["image_url"] = Field(default="image_url", description="图片类型")


class ChatCompletionContentPartImageEmbedsParam(BaseModel):
    image_embeds: ImageEmbedsParam = Field(..., description="图片相关的参数")
    type: Literal["image_embeds"] = Field(
        default="image_embeds", description="图片类型"
    )
    uuid: str | None = Field(default=None, description="图片嵌入向量的唯一标识符")


class ChatCompletionContentPartTextParam(BaseModel):
    text: str = Field(..., description="文本内容")
    type: Literal["text"] = Field(default="text", description="文本类型")


class ChatCompletionContentPartVideoParam(BaseModel):
    video_url: ImageUrlParam = Field(..., description="视频URL")
    type: Literal["video_url"] = Field(default="video_url", description="视频类型")


class ScoreMultiModalParam(BaseModel):
    content: Sequence[
        ChatCompletionContentPartImageParam
        | ChatCompletionContentPartImageEmbedsParam
        | ChatCompletionContentPartTextParam
        | ChatCompletionContentPartVideoParam
    ] = Field(..., description="重排序的多模态内容")


class _RerankOptions(BaseModel):
    """两个 Qwen score 端点共享的可选请求参数。"""

    use_activation: bool | None = Field(
        default=True,
        description="是否对池化层的输出应用激活函数。如果没有指定，则会使用池化层的默认设置，通常情况下默认值为 True。",
    )
    model: str | None = Field(default=None, description="模型名称")
    user: str | None = Field(default=None, description="用户ID")
    truncate_prompt_tokens: int | None = Field(
        default=None, description="截断提示词的最大 token 数，防止超出模型上下文限制"
    )
    request_id: str | None = Field(default=None, description="请求ID，用于跟踪和调试")
    priority: int | None = Field(
        default=None,
        description="请求的优先级（数值越小表示处理优先级越高；默认值：0）。如果所使用的模型不支持优先级调度，则任何非 0 的优先级都会引发错误。",
    )
    mm_processor_kwargs: dict | None = Field(
        default=None, description="多模态处理器的额外参数"
    )
    cache_salt: str | None = Field(
        default=None,
        description="如果进行了相关设置，前缀缓存将会使用所提供的字符串进行加盐处理，以防止攻击者在多用户环境中猜测提示内容。该盐值应是随机生成的，要受到第三方访问的保护，并且长度要足够长，以确保无法预测（例如，以 base64 编码的 43 个字符，相当于 256 位）。",
    )


class RerankRequest(_RerankOptions):
    query: str | ScoreMultiModalParam = Field(..., description="查询文本或者多模态内容")
    documents: str | ScoreMultiModalParam | Sequence[str | ScoreMultiModalParam] = (
        Field(..., description="文档文本或者多模态内容")
    )
    # vLLM 的 /rerank 不消费 top_n；在适配器上暴露它会让下游误以为能截断结果。


class ScoreQueriesDocumentsRequest(_RerankOptions):
    queries: str | ScoreMultiModalParam | list[str | ScoreMultiModalParam] = Field(
        ..., description="查询文本或者多模态内容"
    )
    documents: str | ScoreMultiModalParam | Sequence[str | ScoreMultiModalParam] = (
        Field(..., description="文档文本或者多模态内容")
    )


class RerankResult(BaseModel):
    """单个候选的重排结果。"""

    index: int = Field(..., ge=0, description="候选在原始 documents 中的索引")
    relevance_score: float = Field(..., description="候选与查询的相关性分数")


class RerankResponse(BaseModel):
    """只建模适配器消费的响应字段，兼容服务端附加 usage 等扩展字段。"""

    results: list[RerankResult] = Field(..., description="重排结果")


class _QwenRerankFormat:
    """共享配置、请求词汇与媒体准入逻辑；不持有 HTTP 客户端。"""

    def _configure(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        image_url_validator: ImageReferenceValidator | None = None,
        local_image_validator: ImageReferenceValidator | None = None,
        max_local_image_bytes: int = DEFAULT_MAX_LOCAL_IMAGE_BYTES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._api_key = api_key
        self._image_url_validator = image_url_validator
        self._local_image_validator = local_image_validator
        self._max_local_image_bytes = max_local_image_bytes

    def _to_provider_input(self, content: ContentInput) -> str | ScoreMultiModalParam:
        """把公共内容块转换成 Qwen 私有请求 DTO。"""
        if isinstance(content, str):
            return content

        parts: list[
            ChatCompletionContentPartTextParam | ChatCompletionContentPartImageParam
        ] = []
        for part in content:
            if isinstance(part, TextContent):
                parts.append(ChatCompletionContentPartTextParam(text=part.text))
                continue
            if isinstance(part, ImageContent):
                resource = part.resource
                # 这里**故意**只把路径/URL 原样搬进 DTO，不在此处校验：准入检查
                # 统一由 `_prepare_inputs` → `_prepare_multimodal_content` 做，
                # 而 同步/异步 `score` 入口都必经它（异步路径还把它放进
                # `run_sync`，因为 DNS 解析与读本地文件都是同步 I/O）。
                #
                # 校验点只有一个，是刻意的：无论调用方走公共的 `ContentInput`
                # 还是直接递供应商 DTO，都落到同一道关卡上，不会出现"两条路两
                # 套策略"。代价是这段读起来像绕过了检查 —— 评审确实这么判过一次，
                # 所以 `test_qwen_reranker.py` 有专门的用例把这条不变量钉住。
                if resource.path is not None:
                    reference = str(resource.path)
                elif resource.url is not None:
                    reference = resource.url
                else:
                    reference = prepare_media_resource(
                        resource,
                        url_validator=self._image_url_validator,
                        local_path_validator=self._local_image_validator,
                        max_local_bytes=self._max_local_image_bytes,
                    )
                parts.append(
                    ChatCompletionContentPartImageParam(
                        image_url=ImageUrlParam(
                            url=reference,
                            detail=ImageDetail(part.detail),
                        )
                    )
                )
                continue
            raise TypeError(f"不支持的多模态内容类型：{type(part).__name__}")
        return ScoreMultiModalParam(content=parts)

    def _prepare_multimodal_content(
        self, data: ScoreMultiModalParam
    ) -> ScoreMultiModalParam:
        """校验多模态类型并把本地图片转换成 Data URL。"""
        allowed_content_types = (
            ChatCompletionContentPartTextParam,
            ChatCompletionContentPartImageParam,
        )
        unsupported_type_msg = f"{self.__class__.__name__} | _prepare_multimodal_content：暂不支持 ChatCompletionContentPartImageEmbedsParam / ChatCompletionContentPartVideoParam 类型"

        prepared_content = []
        for content in data.content:
            if not isinstance(content, allowed_content_types):
                raise CometRAGException(unsupported_type_msg)

            if isinstance(content, ChatCompletionContentPartImageParam):
                image_url = prepare_image_reference(
                    content.image_url.url,
                    url_validator=self._image_url_validator,
                    local_path_validator=self._local_image_validator,
                    max_local_bytes=self._max_local_image_bytes,
                )
                content = content.model_copy(
                    update={
                        "image_url": content.image_url.model_copy(
                            update={"url": image_url}
                        )
                    }
                )
            prepared_content.append(content)
        return data.model_copy(update={"content": prepared_content})

    def _prepare_inputs(
        self,
        query: str | ScoreMultiModalParam,
        documents: str | ScoreMultiModalParam | Sequence[str | ScoreMultiModalParam],
    ) -> tuple[
        str | ScoreMultiModalParam,
        str | ScoreMultiModalParam | Sequence[str | ScoreMultiModalParam],
    ]:
        if isinstance(query, ScoreMultiModalParam):
            query = self._prepare_multimodal_content(query)
        if isinstance(documents, ScoreMultiModalParam):
            documents = self._prepare_multimodal_content(documents)
        elif isinstance(documents, Sequence) and not isinstance(documents, str):
            documents = [
                self._prepare_multimodal_content(doc)
                if isinstance(doc, ScoreMultiModalParam)
                else doc
                for doc in documents
            ]
        return query, documents

    def _build_rerank_request(
        self,
        query: str | ScoreMultiModalParam,
        documents: str | ScoreMultiModalParam | Sequence[str | ScoreMultiModalParam],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return RerankRequest(
            model=self._model_name,
            query=query,
            documents=documents,
            **kwargs,
        ).model_dump(exclude_none=True)

    @staticmethod
    def _document_count(
        documents: str | ScoreMultiModalParam | Sequence[str | ScoreMultiModalParam],
    ) -> int:
        if isinstance(documents, (str, ScoreMultiModalParam)):
            return 1
        return len(documents)

    def _extract_scores(
        self,
        response_json: dict[str, Any],
        *,
        expected_count: int,
    ) -> list[float]:
        response = RerankResponse.model_validate(response_json)
        indexes = [result.index for result in response.results]
        if len(indexes) != len(set(indexes)):
            raise ValueError("重排响应包含重复的候选索引，结果无法可靠对齐")
        if len(indexes) != expected_count:
            raise ValueError(
                f"重排响应包含 {len(indexes)} 个结果，但请求包含 {expected_count} 个候选"
            )
        if sorted(indexes) != list(range(expected_count)):
            raise ValueError("重排响应的候选索引不连续，结果无法可靠对齐")
        return [
            result.relevance_score
            for result in sorted(response.results, key=lambda result: result.index)
        ]


class Qwen3VLReranker(_QwenRerankFormat, BaseReranker[str | ScoreMultiModalParam]):
    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
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
            image_url_validator,
            local_image_validator,
            max_local_image_bytes,
        )
        self._owns_client = client is None
        self.client = client if client is not None else Client(timeout=60.0)

    def _post(self, rerank_request: dict[str, Any]) -> dict[str, Any]:
        rerank_response = self.client.post(
            url=f"{self._base_url}/rerank",
            json=rerank_request,
            headers={
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        rerank_response.raise_for_status()
        return rerank_response.json()

    def _score(
        self,
        query: str | ScoreMultiModalParam,
        documents: str | ScoreMultiModalParam | Sequence[str | ScoreMultiModalParam],
        **kwargs: Any,
    ) -> list[float]:
        try:
            query, documents = self._prepare_inputs(query, documents)
            rerank_request = self._build_rerank_request(query, documents, **kwargs)
            response_json = self._post(rerank_request)
            return self._extract_scores(
                response_json,
                expected_count=self._document_count(documents),
            )
        except CometRAGException:
            raise
        except Exception as e:
            error_msg = (
                f"{self.__class__.__name__} | score | 方法操作发生非预期错误：{str(e)}"
            )
            raise CometRAGException(error_msg) from e

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class AsyncQwen3VLReranker(
    _QwenRerankFormat, AsyncBaseReranker[str | ScoreMultiModalParam]
):
    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
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
            image_url_validator,
            local_image_validator,
            max_local_image_bytes,
        )
        self._owns_client = client is None
        self.client = client if client is not None else AsyncClient(timeout=60.0)

    async def _post(self, rerank_request: dict[str, Any]) -> dict[str, Any]:
        rerank_response = await self.client.post(
            url=f"{self._base_url}/rerank",
            json=rerank_request,
            headers={
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        rerank_response.raise_for_status()
        return rerank_response.json()

    async def _score(
        self,
        query: str | ScoreMultiModalParam,
        documents: str | ScoreMultiModalParam | Sequence[str | ScoreMultiModalParam],
        **kwargs: Any,
    ) -> list[float]:
        try:
            # DNS 与本地文件读取都是同步 I/O，不能在事件循环上执行。
            query, documents = await run_sync(self._prepare_inputs, query, documents)
            rerank_request = self._build_rerank_request(query, documents, **kwargs)
            response_json = await self._post(rerank_request)
            return self._extract_scores(
                response_json,
                expected_count=self._document_count(documents),
            )
        except CometRAGException:
            raise
        except Exception as e:
            error_msg = (
                f"{self.__class__.__name__} | score | 方法操作发生非预期错误：{str(e)}"
            )
            raise CometRAGException(error_msg) from e

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
