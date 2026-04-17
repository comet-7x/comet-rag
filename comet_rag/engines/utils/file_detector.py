from typing import BinaryIO

from magika import Magika

_magika_instance = None


def get_magika():
    global _magika_instance
    if _magika_instance is None:
        _magika_instance = Magika()
    return _magika_instance


def detect_content_type_from_bytes(content: bytes) -> str:
    """输入二进制流，返回类型标签"""
    magika = get_magika()
    result = magika.identify_bytes(content)
    return result.output.ct_label


def detect_content_type_from_path(path: str) -> str:
    """输入文件路径，返回类型标签"""
    magika = get_magika()
    result = magika.identify_path(path)
    return result.output.ct_label


def detect_content_type_from_stream(stream: BinaryIO) -> str:
    """输入文件流，返回类型标签"""
    magika = get_magika()
    result = magika.identify_stream(stream)
    return result.output.ct_label
