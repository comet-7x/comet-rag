import base64
import mimetypes
import os
from pathlib import Path


def to_base64_url(binary_data: bytes, mime_type: str = "image/png") -> str:
    """将二进制流转换为可以直接用于模型输入的 Base64 Data URL"""
    base64_str = base64.b64encode(binary_data).decode("utf-8")
    return f"data:{mime_type};base64,{base64_str}"


def from_base64_url(base64_url: str) -> bytes:
    """将 Base64 Data URL 转换为二进制流"""
    base64_str = base64_url.split(",", 1)[-1]
    return base64.b64decode(base64_str)


def guess_mime_type(image_path: str | Path) -> str:
    """根据文件名解析图片 MIME 类型。"""
    mime_type, _ = mimetypes.guess_type(str(image_path))
    if mime_type is None or not mime_type.startswith("image/"):
        raise ValueError(f"无法确定图片 {image_path} 的 MIME 类型")
    return mime_type


def image_to_base64(
    image_path: str | Path,
    *,
    max_bytes: int | None = None,
) -> str:
    """将本地图片转换为模型可消费的 Base64 Data URL。

    ``max_bytes`` 同时检查文件大小和实际读取值，避免超大文件在 Base64
    膨胀前就占满内存。
    """
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError(f"max_bytes 必须大于 0，收到 {max_bytes}")

    path = Path(image_path).expanduser()
    mime_type = guess_mime_type(path)
    with path.open("rb") as file:
        if max_bytes is None:
            data = file.read()
        else:
            file_size = os.fstat(file.fileno()).st_size
            if file_size > max_bytes:
                raise ValueError(f"图片大小超过上限 {max_bytes} bytes：{path}")
            data = file.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"图片大小超过上限 {max_bytes} bytes：{path}")
    if not data:
        raise ValueError(f"图片文件为空：{path}")
    return to_base64_url(data, mime_type)
