class CometRAGException(Exception):
    """Comet-RAG 基础异常"""

    def __init__(self, message: str = "Comet-RAG error"):
        self.message = message
        super().__init__(self.message)


class ResourceNotFoundError(CometRAGException):
    """资源未找到"""

    def __init__(self, resource: str, identifier: str):
        super().__init__(f"{resource} not found: {identifier}")


class ServiceUnavailableError(CometRAGException):
    """服务不可用"""

    def __init__(self, service: str):
        super().__init__(f"Service unavailable: {service}")
