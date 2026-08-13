from typing import BinaryIO

from magika import Magika

_magika_instance = None


def get_magika() -> Magika:
    global _magika_instance
    if _magika_instance is None:
        _magika_instance = Magika()
    return _magika_instance


def _first_extension(ct_label: str, extensions: list[str]) -> str:
    return extensions[0] if extensions else ct_label


def detect_content_type_from_bytes(content: bytes) -> str:
    """输入二进制内容，返回标准文件扩展名"""
    result = get_magika().identify_bytes(content)
    return _first_extension(result.output.ct_label, result.output.extensions)


def detect_content_type_from_path(path: str) -> str:
    """输入文件路径，返回标准文件扩展名"""
    result = get_magika().identify_path(path)
    return _first_extension(result.output.ct_label, result.output.extensions)


def detect_content_type_from_stream(stream: BinaryIO) -> str:
    """输入文件流，返回标准文件扩展名"""
    result = get_magika().identify_stream(stream)
    return _first_extension(result.output.ct_label, result.output.extensions)
