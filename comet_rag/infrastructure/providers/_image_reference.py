"""模型适配器共享的图片引用解析。

远程 URL 由部署侧策略限制 SSRF；本地路径由部署侧策略限制可读目录，并在当前
进程内转换为 Base64 Data URL。模型服务永远不会收到只对应用主机有效的路径。
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from comet_rag.engines.utils import image_to_base64
from comet_rag.ports import MediaResource

ImageReferenceValidator = Callable[[str], None]
DEFAULT_MAX_LOCAL_IMAGE_BYTES = 20 * 1024 * 1024


def prepare_image_reference(
    image_reference: str,
    *,
    url_validator: ImageReferenceValidator | None,
    local_path_validator: ImageReferenceValidator | None,
    max_local_bytes: int = DEFAULT_MAX_LOCAL_IMAGE_BYTES,
) -> str:
    """校验并规范化 URL、Data URL 或本地图片路径。"""
    if image_reference.startswith("data:image/"):
        return image_reference

    parsed = urlsplit(image_reference)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        if url_validator is not None:
            url_validator(image_reference)
        return image_reference

    if parsed.scheme and len(parsed.scheme) > 1:
        raise ValueError(f"不支持的图片引用协议：{parsed.scheme}://")

    if local_path_validator is not None:
        local_path_validator(image_reference)
    path = Path(image_reference).expanduser()
    if not path.is_file():
        raise ValueError(f"本地图片不存在或不是文件：{path}")
    return image_to_base64(path, max_bytes=max_local_bytes)


def prepare_media_resource(
    resource: MediaResource,
    *,
    url_validator: ImageReferenceValidator | None,
    local_path_validator: ImageReferenceValidator | None,
    max_local_bytes: int = DEFAULT_MAX_LOCAL_IMAGE_BYTES,
) -> str:
    """将明确来源的媒体资源转换为模型端可消费的 URL 或 Data URL。"""
    if resource.path is not None:
        return prepare_image_reference(
            str(resource.path),
            url_validator=url_validator,
            local_path_validator=local_path_validator,
            max_local_bytes=max_local_bytes,
        )
    if resource.url is not None:
        return prepare_image_reference(
            resource.url,
            url_validator=url_validator,
            local_path_validator=local_path_validator,
            max_local_bytes=max_local_bytes,
        )

    data = resource.data
    if data is None:  # MediaResource 自身已校验；此分支只帮助类型检查器收窄。
        raise ValueError("媒体资源缺少来源")
    if len(data) > max_local_bytes:
        raise ValueError(
            f"图片大小超过上限 {max_local_bytes} bytes（实际 {len(data)} bytes）"
        )
    mimetype = resource.mimetype or ""
    if not mimetype.startswith("image/"):
        raise ValueError(f"图片媒体类型必须以 image/ 开头，收到 {mimetype!r}")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mimetype};base64,{encoded}"


__all__ = [
    "DEFAULT_MAX_LOCAL_IMAGE_BYTES",
    "ImageReferenceValidator",
    "prepare_image_reference",
    "prepare_media_resource",
]
