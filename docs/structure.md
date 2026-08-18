# 目录结构与核心流程

本文回答两个问题：**东西放在哪**，以及**一次请求怎么走完全程**。

图里画的是**实际的依赖边**（用 AST 扫出来的），不是理想中的分层。两者不一致
的地方在文末「已知的接缝」里列出来了 —— 一份画着理想图的架构文档，正是本项目
踩过的那种坑：文档说的规则和守卫执行的规则不是同一条，中间的缝刚好够放一个错误。

## 目录

```
comet_rag/
├── api/                HTTP 入口（FastAPI）
│   ├── routes/         ingest · search · kb · tasks · admin
│   ├── deps.py         依赖注入：路由只从 Context 取，绝不自己 new
│   └── lifespan.py     启动装配 / 优雅关停
├── workers/            跨进程部署的消费端（arq）
│   ├── embedder.py     IO 道次：调模型服务
│   ├── preprocessor.py CPU 道次：解析、切分
│   └── maintenance.py  租约回收（**单进程模式绝不能加载**）
├── core/               组合根 + 横切设施（见文末「接缝」）
│   ├── bootstrap.py    按配置装配全套资源；模型只在这里被 new
│   ├── context.py      长生命周期资源的唯一持有者，逆序关停
│   ├── concurrency.py  进程级并发闸门 Gate
│   ├── degradation.py  分级降级控制器
│   └── logging.py      横切：被各层依赖
├── services/           用例编排
│   ├── ingestion.py    IngestRunner：extracting → chunking → indexing
│   ├── retrieval.py    RetrievalService：召回 → 重排
│   ├── knowledge_base.py
│   └── source_policy.py 入库来源准入（SSRF、本地路径、大小上限）
├── schemas/            HTTP 请求/响应 DTO
├── tasks/              通用任务框架（与 RAG 无关，可单独复用）
│   ├── store.py / store_postgres.py    任务状态持久化
│   ├── executor.py / executor_arq.py   执行与重试
│   └── runner.py       StagePipeline：分阶段、可移交道次、可断点续跑
├── infrastructure/     外部世界的适配器
│   ├── providers/      供应商模型服务客户端
│   │   ├── embedding/  OpenAI 兼容 · Qwen3-VL
│   │   ├── reranker/   Qwen3-VL
│   │   └── vision/ llm/ ocr/
│   ├── vectorstore/    Milvus · 内存
│   ├── database/       SQLAlchemy 会话与 ORM 表
│   └── loaders/        S3
├── engines/            纯计算 ← 「库」就是这一层
│   ├── loaders/        本地 · URL · 自动路由
│   ├── parsers/        docx（含 OMML 公式）
│   ├── cleaners/       docx → markdown / blocks
│   ├── chunkers/       文本 · 结构化 · 代码
│   ├── converters/     编码探测 · 压缩包防护
│   ├── pipelines/      Pipeline：串起上面几步，四种运行入口
│   └── embedding/      批量排程（切块 + 限流），不含任何 IO
├── ports/              契约与词汇表 ← 零依赖地基
│   ├── embedding.py    EmbeddingPort · MultimodalEmbeddingPort
│   ├── reranker.py     RerankerPort
│   ├── gate.py         AsyncGate
│   └── content.py      MediaResource · ContentInput · RerankDocument …
├── config/             YAML + 环境变量
└── exceptions/
```

## 模块依赖

```mermaid
flowchart TD
    subgraph SVC["参考服务"]
        api["api/<br/>路由 · 依赖注入"]
        wrk["workers/<br/>arq 消费进程"]
        boot["core/bootstrap · context<br/>组合根"]
    end

    subgraph UC["用例编排"]
        svc["services/<br/>ingestion · retrieval · kb"]
        sch["schemas/<br/>HTTP DTO"]
    end

    subgraph EXT["外部世界 · 通用框架"]
        infra["infrastructure/<br/>providers · vectorstore · database"]
        tsk["tasks/<br/>store · executor · runner"]
        pol["core/concurrency · degradation<br/>闸门 · 降级"]
    end

    lib["engines/<br/>loaders · parsers · cleaners · chunkers<br/>pipelines · embedding 排程"]

    subgraph BASE["零依赖地基"]
        ports["ports/<br/>Protocol 契约 + 值对象"]
        cfg["config/"]
        exc["exceptions/"]
        log["core/logging · tracing<br/>横切"]
    end

    api --> svc
    api --> tsk
    api --> sch
    wrk --> tsk
    boot --> svc
    boot --> infra
    boot --> tsk
    boot --> lib
    svc --> lib
    svc --> infra
    svc --> tsk
    svc --> ports
    svc --> pol
    infra --> lib
    infra --> ports
    tsk --> infra
    lib --> ports

    svc -.-> log
    infra -.-> log
    tsk -.-> log

    classDef base fill:#eef,stroke:#88a
    class ports,cfg,exc,log base
```

**依赖只能向下。** `tests/unit/test_layering.py` 用 AST 强制三条：

| # | 规则 | 破了会怎样 |
|---|------|-----------|
| 1 | `engines/` 不得 import redis / pymilvus / sqlalchemy / arq / fastapi … | 装一个 docx 解析器要拖进一整套中间件，「库」那一半作废 |
| 2 | `engines/` 只能 import `engines` 和 `ports`（**白名单**） | 底层反向依赖上层，`ports/` 存在的意义消失 |
| 3 | `services/` 与 `engines/` 不得 import `infrastructure.providers` | 供应商细节泄漏到用例，换模型要改业务代码 |

第 2 条原本是黑名单（禁 api / workers / services）。黑名单只拦得住已经想到的
那几个 —— 后来新增的 `application/` 就从缝里溜了进去。白名单没有这个失效模式：
新包默认被拒。

## 入库流程

```mermaid
flowchart TD
    A["POST /ingest"] --> P{"来源准入<br/>SourcePolicy"}
    P -->|"拒绝：内网地址 / 本地路径 / 超限"| PX["400，任务都不建"]
    P -->|通过| B["TaskService.submit"]
    B --> C[("TaskStore<br/>内存 / Postgres")]
    C --> D["TaskExecutor<br/>进程内 / arq"]
    D --> E["IngestRunner"]

    subgraph ST["三个阶段：各自可重试、可断点续跑"]
        E --> F["extracting · CPU 道<br/>AutoLoader → parser → cleaner"]
        F --> G["chunking · CPU 道<br/>chunker"]
        G -.->|"Handoff 移交道次"| H["indexing · IO 道"]
    end

    H --> KB{"知识库存在？<br/>模型与建库时一致？"}
    KB -->|否| KBX["失败：不允许跨模型混写同一集合"]
    KB -->|是| I["engines/embedding/batch<br/>按 batch_limit 切块<br/>max_concurrency 控并发"]
    I --> GATE{{"进程级闸门 Gate"}}
    GATE --> J["EmbeddingPort.aembed_batch"]
    J --> K["providers/embedding<br/>Qwen · OpenAI 兼容"]
    K --> L[("VectorStore.aupsert<br/>Milvus / 内存")]
    L --> M["Done：写入条数、chunk 数"]
```

窗口（`embed_batch_size`）、每请求条数（`batch_limit`）、并发数
（`max_concurrency`）是**三个不同的旋钮**，见 `docs/model_usage.md`。

## 检索流程

```mermaid
flowchart TD
    A["POST /search"] --> B["RetrievalService.search"]
    B --> DEG{"当前降级级别"}
    DEG -->|"NO_RERANK"| D1["关掉重排"]
    DEG -->|"更低"| D2["再砍 top_k"]
    DEG -->|NORMAL| D3["按请求执行"]

    D1 --> E["aembed_query"]
    D2 --> E
    D3 --> E
    E --> GATE{{"进程级闸门<br/>与 embedding 共用"}}
    GATE --> F[("VectorStore.asearch<br/>取 fetch_k 条粗召回")]
    F --> G{"命中为空？"}
    G -->|是| GX["返回空结果，附带 effective_top_k 与降级级别"]
    G -->|否| H{"配了 reranker<br/>且允许重排？"}
    H -->|否| I["直接截取 top_k"]
    H -->|是| J["RerankerPort.arank"]
    J --> K{"分数条数与索引<br/>能与候选对齐？"}
    K -->|"否 / 抛异常"| L["降级：返回向量召回结果<br/>并打 WARNING"]
    K -->|是| M["按新分数排序 → top_k"]
```

**重排失败一定降级、不失败整个查询** —— 检索是读路径，稍差的结果远好过没有
结果。但降级必须留下日志，否则线上质量下滑无人察觉。

## 改一件事，去哪找

| 想做的事 | 位置 |
|---|---|
| 接一个新的 embedding 服务 | `infrastructure/providers/embedding/`，继承 `BaseEmbeddingModel`，在 `core/bootstrap.py` 装配 |
| 改「一次请求发几条、几个并发」 | `engines/embedding/batch.py` |
| 改 embedding 契约本身 | `ports/embedding.py`（会波及所有适配器，pyright 会告诉你哪些） |
| 加一种文件格式 | `engines/parsers/` + `engines/pipelines/hooks.py` 注册 |
| 改切分策略 | `engines/chunkers/` |
| 改并发上限 / 背压 | `core/concurrency.py`，配置在 `config/schemas.py` 的 `LimitsConfig` |
| 改降级策略 | `core/degradation.py` |
| 加一个 HTTP 端点 | `api/routes/` + `schemas/` |
| 改任务重试 / 断点续跑 | `tasks/runner.py`、`tasks/executor.py` |

## 已知的接缝

诚实记录，避免下一个人（或下一次重构）踩进去。

**`core/` 其实是三样东西。** 依赖图里 `core` 的箭头方向看着自相矛盾，因为它
同时装着：

- **组合根**（`bootstrap.py`、`context.py`）—— 在最顶层，依赖所有人；
- **策略对象**（`concurrency.py`、`degradation.py`）—— 在中层，被 `services/` 依赖；
- **横切设施**（`logging.py`、`tracing.py`）—— 在最底层，被 `services`、
  `infrastructure`、`tasks` 依赖。

所以「`services/` 不得依赖 `core/`」这条规则**没法一刀切**，也就没有守卫。
真要收干净，应当把横切设施单独提出来。目前记为已知债务。

**`tasks/` 依赖 `infrastructure/database`。** `store_postgres.py` 需要会话与
ORM 表。两者在分层图里同级，不构成反向依赖，但确实让「任务框架可单独复用」
这句话打了折 —— 复用时得连 `infrastructure/database` 一起拿。
