from enum import StrEnum

from pydantic import BaseModel, Field


class Env(StrEnum):
    """环境枚举"""

    TEST = "test"  # 个人使用
    DEV = "dev"  # 开发
    PROD = "prod"  # 生产


class ServerConfig(BaseModel):
    app_name: str = Field(default="Comet-RAG", description="应用名称")
    env: Env = Field(default=Env.TEST, description="项目运行环境")
    host: str = Field(default=..., description="服务监听地址")
    port: int = Field(..., description="服务监听端口")


class SqlDatabaseConfig(BaseModel):
    """SQL 数据库配置 (如 MySQL, PostgreSQL)"""

    host: str = Field(..., description="数据库主机地址")
    port: int = Field(..., description="端口号")
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")  # 生产环境建议用 SecretStr
    database: str = Field(..., description="数据库名称")
    connect_timeout: int = Field(30, description="连接超时时间(秒)")


class RedisConfig(BaseModel):
    """Key-Value 数据库配置 (通常指 Redis)"""

    host: str = Field(..., description="主机地址")
    port: int = Field(..., description="端口号")
    password: str | None = Field(None, description="密码")
    db_index: int = Field(0, description="数据库索引编号")
    timeout: int = Field(10, description="超时时间")


class MongoConfig(BaseModel):
    """文档数据库配置 (如 MongoDB)"""

    host: str = Field(..., description="主机地址")
    port: int = Field(..., description="端口号")


class VectorDatabaseConfig(BaseModel):
    """向量数据库配置 (如 Milvus, Pinecone)"""

    endpoint: str = Field(..., description="服务接入点地址")
    api_key: str | None = Field(None, description="API 访问密钥")
    collection_name: str = Field(..., description="集合/数据库名称")


class S3Config(BaseModel):
    """对象存储配置 (S3 标准)"""

    endpoint_url: str = Field(..., description="S3 服务地址")
    access_key: str = Field(..., description="访问密钥 ID")
    secret_key: str = Field(..., description="私钥")
    bucket_name: str = Field(..., description="存储桶名称")
    region: str | None = Field(None, description="地域，如 cn-north-1")


class APPConfig(BaseModel):
    server_config: ServerConfig = Field(..., description="服务配置")
