"""配置管理"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置"""

    app_name: str = "Comet-RAG"
    debug: bool = False

    # 数据库配置
    database_url: str = "sqlite:///./comet_rag.db"

    # LLM 配置
    llm_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # Embedding 配置
    embedding_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""

    # Reranker 配置
    reranker_url: str = ""
    reranker_api_key: str = ""
    reranker_model: str = ""

    # VectorStore 配置
    vectorstore_uri: str = ""
    vectorstore_db_name: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """获取配置（单例）"""
    return Settings()
