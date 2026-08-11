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
    pool_size: int = Field(10, gt=0, description="连接池常驻连接数")
    max_overflow: int = Field(20, ge=0, description="峰值时允许超出的连接数")
    echo: bool = Field(False, description="打印 SQL，仅调试用")

    @property
    def dsn(self) -> str:
        """异步 DSN。驱动写死 asyncpg —— 同步驱动在 async 引擎里会静默阻塞
        整个事件循环，且症状是"偶尔很慢"而非报错，极难定位。"""
        return (
            f"postgresql+asyncpg://{self.username}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )


class RedisConfig(BaseModel):
    """Key-Value 数据库配置 (通常指 Redis)"""

    host: str = Field(..., description="主机地址")
    port: int = Field(..., description="端口号")
    password: str | None = Field(None, description="密码")
    db_index: int = Field(0, description="数据库索引编号")
    timeout: int = Field(10, description="超时时间")

    @property
    def url(self) -> str:
        """DSN 形式。arq 只认这个，拼接放在这里而不是调用点 ——
        密码要 URL 编码这种细节只该有一处。"""
        from urllib.parse import quote  # noqa: PLC0415

        auth = f":{quote(self.password, safe='')}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db_index}"


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


class ModelConfig(BaseModel):
    """模型配置 (如 OpenAI, Cohere)"""

    base_url: str = Field(..., description="服务地址")
    model_name: str = Field(..., description="模型名称")
    api_key: str | None = Field(None, description="API 访问密钥")


class EmbeddingModelConfig(ModelConfig):
    """Embedding 模型。比通用模型多一个**必填**的维度。

    维度不是可选信息：建 collection 要它，写入校验要它（spec A12）。
    留空等到运行时才发现不匹配，那时向量库里已经灌进脏数据了。
    """

    dim: int = Field(..., gt=0, description="向量维度，必须与模型实际输出一致")


class Backend(StrEnum):
    """后端选择。

    `memory` 让整条链路在零中间件下可跑 —— 测试、本地开发、以及 plan 里
    "先内存后真实"的端到端验证都靠它。生产用真实后端。
    """

    MEMORY = "memory"
    MILVUS = "milvus"
    POSTGRES = "postgres"
    INPROCESS = "inprocess"
    ARQ = "arq"


class BackendsConfig(BaseModel):
    """把"用哪个实现"从代码里挪到配置里。

    换后端只改这里，业务代码一行不动 —— 这是 Phase 4 能"每次只换一个后端、
    端到端测试始终在保护"的前提。
    """

    vector_store: Backend = Field(default=Backend.MEMORY, description="memory | milvus")
    task_store: Backend = Field(default=Backend.MEMORY, description="memory | postgres")
    task_executor: Backend = Field(
        default=Backend.INPROCESS, description="inprocess | arq"
    )
    max_concurrency: int = Field(
        default=8, gt=0, description="执行器同时在跑的任务上限"
    )


class InfrastructureConfig(BaseModel):
    embedding_model: EmbeddingModelConfig = Field(..., description="嵌入模型配置")
    #: 可选：没配就跳过重排，检索仍可用（见 services/retrieval.py）
    reranker: ModelConfig | None = Field(default=None, description="重排序模型配置")
    vector_database: VectorDatabaseConfig | None = Field(
        default=None, description="向量库连接，backends.vector_store 非 memory 时必填"
    )
    database: SqlDatabaseConfig | None = Field(
        default=None, description="关系库连接，backends.task_store=postgres 时必填"
    )
    redis: RedisConfig | None = Field(
        default=None, description="Redis 连接，backends.task_executor=arq 时必填"
    )


class APPConfig(BaseModel):
    server_config: ServerConfig = Field(..., description="服务配置")
    infrastructure_config: InfrastructureConfig = Field(..., description="基础设施配置")
    backends: BackendsConfig = Field(
        default_factory=BackendsConfig, description="各资源用哪个实现"
    )
