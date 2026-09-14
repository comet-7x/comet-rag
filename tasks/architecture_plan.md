# Architecture Evolution Plan：能力边界与统一入口

> 状态：方向已确认，分阶段执行；不得用本计划无边界扩大当前里程碑
> 当前状态：M1～M3、仓库结构归一化及文档规范化前置重构已完成
> 当前优先级：M4-T3 Fixed/Recursive 核心与位置算法
> 最后更新：2026-09-14

## 1. 目的

本计划记录 Comet-RAG 的长期能力边界，避免后续按目录名称或实现数量临时决定
代码位置。它回答四个问题：

1. 哪些能力应定义为 Port；
2. 哪些可替换算法应定义为 Strategy；
3. 哪些跨能力流程应由 Service / Planner 编排；
4. 内部必须分层时，如何仍为库使用者提供一个统一、容易发现的入口。

本计划是目标架构，不是一次性重构清单。任何迁移都必须绑定真实里程碑、契约测试
和兼容方案；不能为了让目录看起来整齐，打断当前可工作的 DOCX/PDF 入库链路。

## 2. 三类抽象

### 2.1 Port：隔离外部系统、资源与生命周期

Port 描述上层需要的稳定能力，不携带供应商请求字段。适合 Port 的能力通常满足：

- 实现涉及网络、数据库、对象存储、模型 SDK、文件句柄或长期连接；
- 已有两个实现，或明确需要真实实现与内存/测试实现互换；
- 调用者不应知道 OpenAI、MinerU、Milvus、S3 等供应商；
- 可以写出所有实现共用的行为契约；
- 可以统一成功结果、错误类别、并发与关闭语义。

Port 放在 `comet_rag/ports/`，只依赖标准库和自己的值对象。每个新 Port 必须同时
回答：谁调用、谁实现、资源由谁关闭、哪些错误可重试、契约测试在哪里。

### 2.2 Strategy：替换纯计算算法

Strategy 处理进程内、确定性的算法替换，例如固定长度切分、递归切分、RRF 融合。
它可以用 `Protocol`，但不因使用了 `Protocol` 就成为架构 Port。Strategy 通常：

- 不持有网络连接或外部 SDK；
- 输入相同即可稳定复现；
- 不负责配置装配、重试、限流或资源关闭；
- 与算法所在的 `engines/` 子模块一起演进。

Strategy 应按能力命名，例如 `ChunkingStrategy`、`FusionStrategy`，避免所有抽象都
带 `Port` 后缀，最终失去“跨层边界”这一语义。

### 2.3 Service / Planner：编排多个 Port 与 Strategy

Service 实现用例，Planner 生成后续执行所需的结构化计划。它们负责顺序、分支、
降级和结果组合，但不直接实现外部协议或底层算法。例如：

- `IngestionService` 编排 Loader、Extractor、Chunker、Embedding 与 Store；
- `RetrievalService` 编排向量召回、关键词召回、融合与重排；
- `IndexPlanner` 生成父块、子块及关系，不自己调用 Milvus；
- `HierarchyBuilder` 根据 Chunk 构建层级，不负责 Embedding HTTP 请求。

## 3. 目标整体链路

```text
SourceLoaderPort
    │
    ▼
LoadedResource（受管本地文件、来源信息、媒体类型、释放语义）
    │
    ▼
DocumentExtractorPort
    │
    ▼
ExtractedDocument（提取 Markdown + 文档元数据）
    │
    ▼
DocumentNormalizationStrategy
    │
    ▼
NormalizedDocument（统一 Markdown + 文档元数据）
    │
    ▼
ChunkingStrategy
    │
    ▼
Chunk
    │
    ▼
IndexPlanner / HierarchyBuilder
    │
    ▼
IndexPlan（父子块、关系、待向量化记录）
    │
    ├──► EmbeddingPort ──► VectorStorePort
    ├──► KeywordIndexPort
    └──► KnowledgeExtractorPort / Strategy ──► GraphStorePort

查询：

Query
    ├──► EmbeddingPort ──► VectorSearchPort ──┐
    └──► KeywordSearchPort ───────────────────┤
                                              ▼
                                      FusionStrategy
                                              │
                                              ▼
                                        RerankerPort
                                              │
                                              ▼
                                          SearchHit
```

当前闭环是 `LoadedResource → DocumentExtractorPort → ExtractedDocument →
DocumentNormalizationStrategy → NormalizedDocument → Chunker → Embedding →
VectorStore`。`DocumentBlock` 由 M4 的页面、结构和 IndexPlan 用例反推，不提前建立
没有消费者的万能块模型。

## 4. 各部分的推荐边界

| 能力 | 推荐抽象 | 所属层 | 边界说明 |
|---|---|---|---|
| Local / URL / S3 来源加载 | `SourceLoaderPort` | `ports` 契约；各层实现 | 输入来源，输出受管本地资源；统一释放语义 |
| 来源选择 | `LoaderRoute` / Loader router | `engines` 或组合层 | 只匹配来源，不执行 I/O，不持有第二份闸门 |
| DOCX、PDF 文档提取 | `DocumentExtractorPort` | `ports` | 本地文件转标准文档；不出现 MinerU URL/响应字段 |
| DOCX 提取 | `DocxDocumentExtractor` | `engines/documents/docx` | 纯本地实现，内部组合 converter/parser/cleaner |
| MinerU 提取 | `MinerUDocumentExtractor` | `infrastructure/extractors` | 外部 HTTP 适配器，负责协议、重试、连接和关闭 |
| 跨格式文档规范化 | `DocumentNormalizationStrategy` | `engines/documents/normalization` | 纯计算且幂等；格式专属清洗不进入这里 |
| 固定、递归、按页、标题切分 | `ChunkingStrategy` | `engines/chunkers` | 纯计算；消费规范文档，生成带位置的平坦块 |
| 语义切分 | `SemanticChunkingService` + `BreakpointStrategy` | `services` + `engines/chunkers` | Service 调 EmbeddingPort；engines 只计算相似度断点 |
| 父子块、邻接关系、索引记录 | `IndexPlanner` / `HierarchyBuilder` | `engines/indexing` | 生成 `IndexPlan`，不直接写后端 |
| 外部 LLM 实体关系抽取 | `KnowledgeExtractorPort` | `ports` + provider | 外部模型调用、错误和资源生命周期 |
| 本地规则实体关系抽取 | `KnowledgeExtractionStrategy` | `engines` | 纯计算，不应伪装成外部 Port |
| 文本/多模态向量化 | `EmbeddingPort` | `ports` | 隔离模型供应商与请求协议 |
| 候选重排 | `RerankerPort` | `ports` | 失败时由检索 Service 降级，不拖垮读路径 |
| ANN 存储与查询 | `VectorStorePort` / 现有 `BaseVectorStore` | 长期下沉 `ports` | 不允许 Milvus 表达式穿透结构化过滤契约 |
| BM25/关键词索引 | `KeywordIndexPort`、`KeywordSearchPort` | `ports` | 隔离 Elasticsearch/Postgres 等后端 |
| RRF、加权融合 | `FusionStrategy` | `engines/retrieval` | 纯计算，不访问存储 |
| 混合检索 | `RetrievalService` | `services` | 并行召回、融合、重排和降级的用例编排 |
| 图存储 | `GraphStorePort` | `ports` | 隔离 Neo4j 等具体查询语言 |
| 任务、知识库持久化 | 现有 Store/Repository 契约 | `tasks` / repository 边界 | 保持已有契约测试，不在 M2 改动 |

## 5. Loader 的统一决策

### 5.1 最终落位

Local、HTTP 与 S3 都读取进程外资源并持有生命周期，现已统一到
`infrastructure/sources/`。格式词汇与内容类型决策是纯规则，单独位于
`engines/documents/formats.py`。S3 SDK 在具体方法内惰性导入，因此统一物理目录
不会破坏 core-only 安装。

`services/pipeline.py` 只接收 `SourceLoaderPort`；面向库用户的
`comet_rag/pipeline.py` 才负责装配默认 Local/HTTP Loader。这样 service 不依赖
具体适配器，`Pipeline()` 的易用性也不丢失。

### 5.2 真正需要统一的是使用入口

已经提供轻量、稳定的公共门面：

```python
from comet_rag.loaders import (
    AutoLoader,
    LoaderRoute,
    LocalLoader,
    URLLoader,
    S3Loader,
)
```

`comet_rag.loaders` 只负责发现性与稳定导出，不拥有实现：

- Local、HTTP 与 AutoLoader 直接导出；
- `S3Loader` 必须惰性导入，只有真正使用它时才要求包含 S3 SDK 的可选依赖组
  （当前为 `server`；是否拆成独立 `s3` extra 另行评审）；
- 服务内部仍由组合根装配全部实现；
- 重构前的内部导入路径已在 `0.1.0` 阶段删除，不保留影子目录。

这样使用者只看一个入口，维护者仍能从目录位置判断依赖方向。公共 API 与物理目录
不必一一对应。

### 5.3 Loader 契约

`ports/source.py` 已提供：

```text
SourceLoaderPort
├── load / aload
├── batch_load / abatch_load（显式并发预算）
└── cleanup / acleanup

LoadedResource
├── path
├── source
├── media_type / file_type / size
└── cleanup（幂等）
```

已完成约束：

1. `LoaderRoute.loader` 面向 `SourceLoaderPort`，而不是要求继承 `BaseLoader`；
2. `BaseLoader` 保留为批量回退、闸门和生命周期的模板实现，不再充当唯一契约；
3. `LoaderContent` 是否改名 `LoadedResource` 必须提供兼容别名，不能直接破坏库 API；
4. Local、URL、S3 跑同一套来源加载契约；供应商专有选项只留在具体实现；
5. 文件类型检测和 metadata 构造从三份 Loader 实现中收敛为共享函数。

## 6. DocumentExtractor 与 Parser 的边界

`DocumentExtractorPort` 是跨格式公共能力；Parser 是某个本地提取器的内部算法步骤：

```text
DocumentExtractorPort
├── DocxDocumentExtractor                 engines/documents/docx/
│   ├── DocxConverter
│   ├── DocxParser
│   └── DocxCleaner
└── MinerUDocumentExtractor               infrastructure/extractors/
    └── mineru-api / mineru-router
```

因此 `DocxParser` 不需要与 MinerU 对称。重构复核确认 `BaseParser` 只有一个实现且
没有多态调用方，已经删除；格式专属 parser/converter/type 统一归入
`engines/documents/<format>/`，不能为了增加实现数量把外部 MinerU 协议放进 engines。

后续文档型 PDF 若由进程内、确定性且无服务生命周期的解析库完成，放在
`engines/documents/pdf/`；新增依赖仍需按 core-only 边界单独评审。PaddleOCR 无论通过
HTTP 服务还是本地重型 SDK 接入，都放在 `infrastructure/extractors/paddleocr/`，因为
它涉及模型权重、计算设备、部署差异或客户端生命周期。PaddleOCR 返回结果中可复用的
纯计算转换规则可以下沉到 `engines/documents/ocr/`。

`ExtractedDocument` 表示提取器映射后的通用结果，`NormalizedDocument` 表示可交给
Chunker 的规范结果；二者之间统一经过 `DocumentNormalizationStrategy`。未来支持
按页、bbox、图片资产或表格结构时，应由 M4 规格扩展 `DocumentBlock`，不能把 MinerU
原始响应直接塞进通用值对象。

## 7. Chunking、Hierarchy 与 Graph 的拆分

### 7.1 页面与版面属于 Extraction

页码、bbox、标题层级、表格和公式类型来自格式解析/OCR。Extractor 负责产出这些
事实；Chunker 不应重新猜页边界。

### 7.2 文本分割属于 ChunkingStrategy

固定长度、递归分隔符、按页、按标题及 overlap 属于纯切分策略。策略消费
`NormalizedDocument` / 后续 `DocumentBlock`，输出平坦 Chunk，不写数据库。

完整语义分块不是纯 Strategy：它需要通过 `EmbeddingPort` 发起外部调用，并受模型并发、
失败和生命周期约束。该流程由 `SemanticChunkingService` 编排；只有句间相似度与断点选择
这类确定性计算进入 engines。详细契约见 `tasks/m4_spec.md`。

### 7.3 父子块属于 IndexPlanner

父子块同时涉及两种粒度、`parent_id`、返回粒度和写入记录，已经超出 `split()`。
`IndexPlanner` 应输出显式 `IndexPlan`，说明哪些块向量化、哪些块只用于回填。

### 7.4 Graph 属于知识提取与图索引

GraphRAG 的实体抽取、关系抽取、消歧、社区构建和图存储不能塞进 `ChunkerPort`。
调用外部 LLM 的部分使用 Port；纯本地算法使用 Strategy；流程由 Graph ingestion
Service 编排。

## 8. Retrieval 的拆分

不建立携带 `mode="hybrid"`、Milvus 表达式和 BM25 专有参数的万能 `SearchPort`。
目标结构是：

```text
RetrievalService
├── VectorSearchPort
├── KeywordSearchPort
├── FusionStrategy
└── RerankerPort
```

- ANN 与 BM25 是两条召回通道；
- Hybrid 是 Service 的编排方式；
- RRF/加权融合是纯计算 Strategy；
- Reranker 是外部模型 Port；
- 对 API 暴露的是稳定 `SearchRequest` / `SearchHit`，不是后端查询语法。

这些接口在 M3 有真实 BM25 方案和过滤需求后再冻结，M2 不预先发明。

## 9. 目标目录形态

```text
comet_rag/
├── loaders/                         # 用户统一入口；稳定门面
│   └── __init__.py
├── ports/
│   ├── source.py                    # 已建立：来源契约与受管资源
│   ├── document.py                  # 提取/规范文档值对象与提取契约
│   ├── vision.py                    # 图片描述模型契约
│   ├── embedding.py
│   ├── reranker.py
│   ├── vector_store.py              # dense / keyword 契约与存储值对象
│   └── graph_store.py               # 后续里程碑
├── engines/
│   ├── documents/
│   │   ├── normalization/           # 跨格式规范化 Strategy
│   │   └── docx/
│   │       ├── extractor.py
│   │       ├── converter.py
│   │       ├── parser.py
│   │       └── cleaner.py
│   ├── chunkers/
│   │   ├── protocol.py
│   │   ├── fixed.py
│   │   ├── recursive.py
│   │   ├── markdown.py
│   │   ├── page.py
│   │   └── breakpoints.py
│   ├── indexing/
│   │   ├── planner.py
│   │   └── hierarchy.py
│   └── retrieval/
│       ├── hybrid.py
│       └── fusion.py
├── infrastructure/
│   ├── sources/                    # Local · HTTP · S3 · AutoLoader
│   ├── extractors/
│   │   └── mineru.py
│   ├── models/                     # embedding · reranker · vision
│   ├── persistence/
│   │   ├── sql/
│   │   ├── vector_store/
│   │   ├── task_store/
│   │   └── knowledge_base/
│   └── task_execution/
│       └── arq.py
├── services/
│   ├── ingestion.py
│   ├── pipeline.py
│   └── retrieval.py
├── api/
│   ├── routes/
│   └── schemas/
└── composition/
    └── bootstrap.py                  # 唯一跨层装配点
```

上图中 M4 的 `chunkers/indexing` 细节仍是目标形态；其余结构归一化已按
`repository_structure_spec.md` 执行，重构前的内部路径已删除。

## 10. 优先级与执行顺序

### P0 — 历史记录：M2 安全闭环（已完成）

1. **M2-T5（已完成）**：MinerU 主路径已具备总 deadline、请求超时、错误分类、
   404 单次重提、响应/Markdown 限长及取消清理。
2. **M2-T6（已完成）**：配置、独立 MinerU 闸门、组合根注册、Task context
   二次限长与逆序关闭均已接通。
3. **M2-T7（已完成）**：三来源 PDF 入库、内容复验及 TaskStore 可观察行为。
4. **M2-T8～T9（已完成）**：真实 MinerU 基准、文档与里程碑验收。

### P1 — M2 完成后：统一公共概念与使用入口（已完成）

1. **已完成**：建立 `DocxDocumentExtractor`，让 DOCX 与 MinerU 在高层共同实现
   `DocumentExtractorPort`；保留现有 DOCX 快照。
2. **已完成**：建立 `SourceLoaderPort` / `LoadedResource`，Local、URL、S3 运行共享契约。
3. **已完成**：增加 `comet_rag.loaders` 惰性统一门面；结构归一化阶段已删除内部旧导入。
4. **已完成**：收敛 Local/URL/S3 的类型检测、metadata 和临时文件生命周期代码。
5. **已完成**：删除没有多态调用方的 `BaseParser`/`BaseConverter`，DOCX 内部步骤按格式聚合。

### P2 — M3：由真实混合检索需求驱动

M3 规格已冻结并完成：Milvus 原生 BM25 位于 `KeywordSearchPort` 后，RRF 是 engines
内的纯计算 Strategy，Hybrid 由 RetrievalService 编排。`BaseVectorStore` 已下沉 ports，
结构归一化阶段已删除内部旧路径；schema v2 不自动删除旧 collection。详细边界与成功标准见
`tasks/m3_spec.md`，实施清单见 `tasks/m3_todo.md`。

### P3 — M4：Chunking 与可选父子索引

M4 先完成带位置的 Chunk 契约、固定/递归策略、Pipeline/Task 单链路和 Markdown/Page
结构感知。`IndexPlan`、`HierarchyBuilder` 与 `DocumentStore` 位于独立 schema 决策门
之后；数据库迁移和向量 metadata 约定未经确认不得实施。GraphRAG 仍不在 M4 范围。
详细顺序见 `tasks/m4_plan.md` 与 `tasks/m4_todo.md`。

## 11. Port 创建门槛

新增 Port 前必须同时检查：

- [ ] 有明确调用者与至少一个真实实现、一个测试实现；
- [ ] 抽象隔离了外部依赖或确有跨实现替换价值；
- [ ] 签名中没有供应商字段和无边界 `**kwargs`；
- [ ] 值对象、错误分类、资源所有权和关闭顺序已定义；
- [ ] 有实现无关的契约测试，并做过反向验证；
- [ ] 组合根是唯一选择具体实现的位置；
- [ ] 不破坏 core-only 安装和依赖方向；
- [ ] 若修改公开接口，已有兼容路径和迁移期限。

不满足这些条件时，优先使用具体类、内部函数或 Strategy，不创建占位 Port。

## 12. 当前决策记录

| 决策 | 结论 |
|---|---|
| Loader 是否物理合并到一个层 | 是；Local/HTTP/S3/AutoLoader 统一在 `infrastructure/sources`，S3 SDK 惰性导入 |
| Loader 是否提供一个用户入口 | 是；M2 后增加 `comet_rag.loaders` 惰性门面 |
| 是否重构全部 Loader | 已完成；契约在 ports，实现统一在 infrastructure，公共入口为 `comet_rag.loaders` |
| MinerU 是否移入 `engines/documents` | 否；它是外部 `DocumentExtractorPort` 适配器 |
| DOCX 是否最终实现同一提取 Port | 是；M2 后 P1 已完成 |
| Chunker 是否统一做页面、父子块和 Graph | 否；分别属于 Extraction、Strategy、Planner 与 Graph ingestion |
| 是否定义 Search/Graph 全套 Port | M3 只定义 Vector/Keyword Search；Graph 仍由后续需求驱动 |
| `BaseParser` 是否删除 | 是；仅有一个实现且没有多态调用方，格式内部步骤不提升为项目级抽象 |
| Loader 是否提供统一公共入口 | 是；`ports/source.py` 是契约，`comet_rag.loaders` 是惰性公共门面 |
| Worker 是否收入 `tasks/` | 否；workers 是独立进程入口，必须与单进程会加载的通用任务框架隔离 |
| DOCX 是否改为垂直目录 | 已完成；专属 converter/parser/cleaner/extractor 位于 `engines/documents/docx` |
| 顶层 Parser/Converter 目录是否保留 | 否；格式专属代码归入 documents，ZIP 防护归入 documents/common |
| Provider 私有辅助模块是否整理 | 已完成；模型适配器位于 `infrastructure/models`，MinerU 位于 `infrastructure/extractors` |
| 完整语义分块是否是纯 Strategy | 否；Service 编排 EmbeddingPort，engines 只保留断点算法 |
| 当前下一项工作 | M4-T3 Fixed/Recursive 核心与位置算法 |
