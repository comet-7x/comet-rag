"""配置模型。

## `Field()` 的写法有一条硬约定

    必填：Field(..., description=…)          ← 位置参数写 Ellipsis
    选填：Field(default=X, description=…)    ← **必须**用 default= 关键字

不是风格洁癖：pyright 按 PEP 681 只认 `default=` / `default_factory=` 这两个
关键字参数，位置参数它一概不解析。于是 `Field(30, …)` 在类型检查器眼里是
**没有默认值的必填字段** —— `SqlDatabaseConfig(host=…, port=…)` 会被报成
"缺少参数 connect_timeout"，而运行时明明好好的。

反过来 `Field(default=...)`（Ellipsis 走关键字）更危险：pyright 会当成
"默认值是 Ellipsis" 的选填字段，于是漏报真正缺参的调用，运行时才炸。
"""

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
    host: str = Field(..., description="服务监听地址")
    port: int = Field(..., description="服务监听端口")


class SqlDatabaseConfig(BaseModel):
    """SQL 数据库配置 (如 MySQL, PostgreSQL)"""

    host: str = Field(..., description="数据库主机地址")
    port: int = Field(..., description="端口号")
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")  # 生产环境建议用 SecretStr
    database: str = Field(..., description="数据库名称")
    connect_timeout: int = Field(default=30, description="连接超时时间(秒)")
    pool_size: int = Field(default=10, gt=0, description="连接池常驻连接数")
    max_overflow: int = Field(default=20, ge=0, description="峰值时允许超出的连接数")
    echo: bool = Field(default=False, description="打印 SQL，仅调试用")

    @property
    def dsn(self) -> str:
        """异步 DSN。驱动写死 asyncpg —— 同步驱动在 async 引擎里会静默阻塞
        整个事件循环，且症状是"偶尔很慢"而非报错，极难定位。

        用户名/密码/库名一律**百分号编码**（PR 评审 #7）。直接拼接的话，
        密码里一个 `@` 或 `/` 就会被解析成 URL 语法：连接串看着没问题、
        报错却是"host 不存在"或直接连到别的库去 —— 而这类密码在生产里很常见。
        用 stdlib 的 `quote` 而不是 SQLAlchemy 的 URL 构造器，是因为本模块属于
        核心包，不能依赖 server extra 里的 sqlalchemy（spec A1）。
        """
        from urllib.parse import quote  # noqa: PLC0415

        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        database = quote(self.database, safe="")
        return (
            f"postgresql+asyncpg://{user}:{password}@{self.host}:{self.port}/{database}"
        )


class RedisConfig(BaseModel):
    """Key-Value 数据库配置 (通常指 Redis)"""

    host: str = Field(..., description="主机地址")
    port: int = Field(..., description="端口号")
    password: str | None = Field(default=None, description="密码")
    db_index: int = Field(default=0, description="数据库索引编号")
    timeout: int = Field(default=10, description="超时时间")

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
    api_key: str | None = Field(default=None, description="API 访问密钥")
    collection_name: str = Field(..., description="集合/数据库名称")


class ModelConfig(BaseModel):
    """模型配置 (如 OpenAI, Cohere)"""

    base_url: str = Field(..., description="服务地址")
    model_name: str = Field(..., description="模型名称")
    api_key: str | None = Field(default=None, description="API 访问密钥")


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


class IngestPolicyConfig(BaseModel):
    """入库来源准入（spec §7 Never 之外的一条硬边界，PR 评审 #4）。

    `POST /ingest` 的 `source` 是调用方给的字符串，会被直接交给 loader ——
    不加约束的话，服务能读到的文件、能连到的网络，调用方都能拿到
    （任意文件读取 + SSRF）。**默认是拒绝**，需要的能力显式打开。
    """

    allow_local: bool = Field(
        default=False,
        description="是否允许从**服务器本地路径**入库。默认关：这是危险能力，"
        "开了之后调用方能让服务读取它权限内的任意文件",
    )
    local_roots: list[str] = Field(
        default_factory=list,
        description="allow_local 时限定的根目录（会展开符号链接后做包含性检查）。"
        "为空 = 整个文件系统，生产环境务必配置",
    )
    allow_url: bool = Field(default=True, description="是否允许从 http/https 入库")
    allow_private_network: bool = Field(
        default=False,
        description="是否允许访问私网/环回/链路本地地址。**默认关 —— 这是挡 SSRF "
        "的主力**，云上元数据服务（169.254.169.254）正是靠它挡住的",
    )
    allowed_url_hosts: list[str] = Field(
        default_factory=list,
        description="URL 主机白名单。为空 = 不限（私网仍被上一条挡着）",
    )
    max_download_bytes: int = Field(
        default=100 * 1024 * 1024,
        gt=0,
        description="单个 URL 响应的最大字节数。Content-Length 与实际流量都会校验",
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
    ingest_policy: IngestPolicyConfig = Field(
        default_factory=IngestPolicyConfig, description="入库来源准入策略"
    )
