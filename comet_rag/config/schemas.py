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


class LimitsConfig(BaseModel):
    """资源上限（spec S4-1、S4-2）。**每一项都是"有界"这件事的具体落点。**

    默认值按"一块消费级 GPU 上跑一个 vLLM"来定，请按实际服务能力调整 ——
    调大不会更快，只会让请求在服务端排队，反而拖长 p99。
    """

    model_concurrency: int = Field(
        default=8,
        gt=0,
        description="本进程对模型服务的**总**并发上限。注意不是每个任务的，"
        "是整个进程的 —— 前者会随任务数翻倍（见 core/concurrency.py）",
    )
    model_queue: int = Field(
        default=256,
        ge=0,
        description="闸门外允许排队的请求数，0 表示不限。"
        "排队者本身占内存，不设界就是把 OOM 往后推",
    )
    model_wait_timeout: float = Field(
        default=30.0,
        gt=0,
        description="等闸门的最长时间。等太久算过载 —— 上游多半早就超时了",
    )
    max_backlog: int = Field(
        default=1000,
        ge=0,
        description="待执行任务数上限，超了直接拒收（HTTP 429）。0 表示不限。"
        "没有它的话，投递量一大队列就无限堆积",
    )
    degrade_failure_rate: tuple[float, float, float] = Field(
        default=(0.2, 0.5, 0.8),
        description="触发三级降级的失败率阈值（关 rerank / 降 top_k / 拒新任务）",
    )
    degrade_saturation: tuple[float, float, float] = Field(
        default=(2.0, 5.0, 10.0),
        description="触发三级降级的闸门饱和度阈值（等待者数 ÷ 并发上限）",
    )
    degrade_recover_after: float = Field(
        default=30.0,
        gt=0,
        description="降级后要稳定这么久才恢复一级。太短会在阈值边界上来回抖",
    )
    degrade_min_samples: int = Field(
        default=20,
        gt=0,
        description="样本不足这个数时一律判正常。"
        "服务刚起来或流量很低时，头几次失败不该被当成故障",
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
    limits: LimitsConfig = Field(
        default_factory=LimitsConfig, description="并发闸门与背压上限"
    )
