import base64
import mimetypes


def to_base64_url(binary_data: bytes, mime_type: str = "image/png") -> str:
    """将二进制流转换为可以直接用于模型输入的 Base64 Data URL"""
    base64_str = base64.b64encode(binary_data).decode("utf-8")
    return f"data:{mime_type};base64,{base64_str}"


def from_base64_url(base64_url: str) -> bytes:
    """将 Base64 Data URL 转换为二进制流"""
    base64_str = base64_url.split(",", 1)[-1]
    return base64.b64decode(base64_str)


def guess_mime_type(image_path: str) -> str:
    """通过文件名猜测 MIME 类型"""
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        raise ValueError(f"无法确定图片 {image_path} 的 MIME 类型")
    return mime_type


def image_to_base64(image_path: str) -> str:
    """将图片转换为 Base64 编码"""
    mime_type = guess_mime_type(image_path)
    with open(image_path, "rb") as f:
        return to_base64_url(f.read(), mime_type)
