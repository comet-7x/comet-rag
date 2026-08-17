"""模型适配器共享的图片引用校验。

远程图片 URL 最终由模型服务抓取。即使请求不是由当前进程直接发出，也不能把
未经校验的内网地址转交给模型服务，否则会把 SSRF 风险移动到模型节点上。
Base64 data URI 不触发网络访问，可以直接放行。
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from comet_rag.exceptions import CometRAGException

ImageURLValidator = Callable[[str], None]


def validate_image_reference(
    image_url: str,
    *,
    validator: ImageURLValidator | None,
) -> None:
    """校验图片引用格式，并按需执行部署侧 URL 准入策略。"""
    if image_url.startswith("data:image/"):
        return

    parsed = urlsplit(image_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise CometRAGException(
            "图片仅支持 http/https URL 或 data:image/... Base64 数据，不支持本地路径"
        )

    if validator is not None:
        validator(image_url)
