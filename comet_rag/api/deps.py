"""请求级依赖注入"""

# from core.context import Context
from fastapi import Request

# def get_context(request: Request) -> Context:
#     """获取应用上下文"""
#     return request.app.state.ctx


def get_llm(request: Request):
    """获取 LLM 实例"""
    return request.app.state.ctx.llm


def get_embedding(request: Request):
    """获取 Embedding 模型实例"""
    return request.app.state.ctx.embedding


def get_reranker(request: Request):
    """获取 Reranker 模型实例"""
    return request.app.state.ctx.reranker


def get_vectorstore(request: Request):
    """获取向量存储实例"""
    return request.app.state.ctx.vectorstore
