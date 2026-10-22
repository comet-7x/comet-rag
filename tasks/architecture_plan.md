# Architecture Evolution Plan：能力边界与统一入口

> 状态：方向已确认，分阶段执行；不得用本计划无边界扩大当前里程碑
> 当前状态：M1～M3、仓库结构归一化及文档规范化前置重构已完成
> 当前进度：V2 已完成现状校准、启动清理与检索边界改造，各批记录见 `tasks/v2_plan.md`；M4 生产闭环仍待完成
> 最后更新：2026-10-22

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

Strategy 处理进程内、确定性的算法替换与组合，例如文档级切分、父子块组织、RRF 融合。
它可以用 `Protocol`，但不因使用了 `Protocol` 就成为架构 Port。Strategy 通常：

- 不持有网络连接或外部 SDK；
- 输入相同即可稳定复现；
- 不负责配置装配、重试、限流或资源关闭；
- 与算法所在的 `engines/` 子模块一起演进。

Chunking 领域再区分两层：`Chunker` 是接收 `str` 的原子分块方式；
`ChunkingStrategy` 基于一个或多个 Chunker 编排完整 `NormalizedDocument`。两者都属于
纯计算，不带 `Port` 后缀。

### 2.3 Service / Planner：编排多个 Port 与 Strategy

Service 实现用例，Planner 生成后续执行所需的结构化计划。它们负责顺序、分支、
降级和结果组合，但不直接实现外部协议或底层算法。例如：

- `IngestionService` 编排 Loader、Extractor、Chunker、Embedding 与 Store；
- `RetrievalService` 编排向量召回、关键词召回、融合与重排；
- `IndexPlanner` 将层级结果物化为带 revision ID 与 metadata 的索引计划，不自己调用 Milvus；
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
Chunker（FixedSize / Recursive / 未来 2+N）
    │
    ▼
ChunkDraft / ChunkHierarchy
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
    ├──► EmbeddingPort ──► AsyncVectorSearchPort ──┐
    └──► AsyncKeywordSearchPort ───────────────────┤
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
DocumentNormalizationStrategy → NormalizedDocument → ChunkingStrategy → Chunker →
Embedding → VectorStore`。M4 已按页面、结构和层级索引用例建立 `DocumentBlock`、
`HierarchyBuilder` 与 `IndexPlan`；revision 入库切片只到 ready，生产链路仍为平坦入库。
图中的独立 KeywordIndex、知识提取与 Graph 能力仍是未来方向，不是现有接口或本批任务。

## 4. 各部分的推荐边界

| 能力 | 推荐抽象 | 所属层 | 边界说明 |
|---|---|---|---|
| Local / URL / S3 来源加载 | `SourceLoaderPort` | `ports` 契约；各层实现 | 输入来源，输出受管本地资源；统一释放语义 |
| 来源选择 | `LoaderRoute` / Loader router | `engines` 或组合层 | 只匹配来源，不执行 I/O，不持有第二份闸门 |
| DOCX、PDF 文档提取 | `DocumentExtractorPort` | `ports` | 本地文件转标准文档；不出现 MinerU URL/响应字段 |
| DOCX 提取 | `DocxDocumentExtractor` | `engines/documents/docx` | 纯本地实现，内部组合 converter/parser/cleaner |
| MinerU 提取 | `MinerUDocumentExtractor` | `infrastructure/extractors` | 外部 HTTP 适配器，负责协议、重试、连接和关闭 |
| 跨格式文档规范化 | `DocumentNormalizationStrategy` | `engines/documents/normalization` | 纯计算且幂等；格式专属清洗不进入这里 |
| 固定、递归等原子分块方式 | `Chunker` | `engines/chunkers` | 纯计算；只消费字符串，生成带相对位置的块；当前 2 种，按真实算法扩展 2+N |
| 按页、标题、父子块组合 | `ChunkingStrategy` | `engines/chunkers` / `engines/indexing` | 消费规范文档与结构事实，编排一个或多个 Chunker |
| 语义切分 | `SemanticChunkingService` + `BreakpointStrategy` | `services` + `engines/chunkers` | Service 调 EmbeddingPort；engines 只计算相似度断点 |
| 父子块、邻接关系、索引记录 | `HierarchyBuilder` / `IndexPlanner` | `engines/indexing` / `services/indexing.py` | 前者计算层级；后者物化 revision ID、metadata 与 `IndexPlan`，不直接写后端 |
| 外部 LLM 实体关系抽取 | `KnowledgeExtractorPort` | `ports` + provider | 外部模型调用、错误和资源生命周期 |
| 本地规则实体关系抽取 | `KnowledgeExtractionStrategy` | `engines` | 纯计算，不应伪装成外部 Port |
| 文本/多模态向量化 | `EmbeddingPort` | `ports` | 隔离模型供应商与请求协议 |
| 候选重排 | `RerankerPort` | `ports` | 失败时由检索 Service 降级，不拖垮读路径 |
| ANN 存储与查询 | `AsyncVectorStore` / `AsyncVectorSearchPort` | `ports/vector_store.py`（已落位） | 不允许 Milvus 表达式穿透结构化过滤契约 |
| BM25/关键词索引 | `AsyncKeywordSearchPort`；写入复用 `AsyncVectorStore` | `ports/vector_store.py`（已落位） | 当前只有 Milvus 原生 BM25 与内存测试实现，不单设 KeywordIndexPort |
| RRF、加权融合 | `FusionStrategy` | `engines/retrieval` | 纯计算，不访问存储 |
| 混合检索 | `RetrievalService` | `services` | 并行召回、融合、重排和降级的用例编排 |
| 图存储 | `GraphStorePort` | `ports` | 隔离 Neo4j 等具体查询语言 |
| 任务、知识库与文档持久化 | `TaskStore` / `KnowledgeBaseRepository` / `DocumentStore` | `tasks/store.py` / `ports/knowledge_base.py` / `ports/document_store.py` | 真实适配器位于 infrastructure，保留共享契约测试 |

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
Chunker 的规范结果；二者之间统一经过 `DocumentNormalizationStrategy`。M4 已通过
`DocumentBlock` 接入真实页面和标题 section 的字符 span；bbox 与资产仍不在当前契约中。
后续结构扩展应由具体用例和规格驱动，不能把 MinerU 原始响应直接塞进通用值对象。

## 7. Chunking、Hierarchy 与 Graph 的拆分

### 7.1 页面与版面属于 Extraction

页码、bbox、标题层级、表格和公式类型来自格式解析/OCR。Extractor 负责产出这些
事实；Chunker 不应重新猜页边界。

### 7.2 原子文本分割属于 Chunker

固定长度和递归分隔符是当前两种原子 Chunker；它们消费 `str`，输出相对于该字符串的
ChunkDraft。语言、格式、size、overlap 与 separators 只是 ChunkProfile，不产生新类。
按页、按标题等文档级 Strategy 消费 `NormalizedDocument` 及其 `DocumentBlock`，
再编排一个或多个 Chunker，不写数据库。

完整语义分块不是纯 Strategy：它需要通过 `EmbeddingPort` 发起外部调用，并受模型并发、
失败和生命周期约束。该流程由 `SemanticChunkingService` 编排；只有句间相似度与断点选择
这类确定性计算进入 engines。详细契约见 `tasks/m4_spec.md`。

### 7.3 父子块计算属于 Strategy，写入决策属于 IndexPlanner

`engines/indexing/HierarchyBuilder` 用父、子两组 Chunker 生成 `ChunkHierarchy`，
保留页面/section 硬边界，不决定存储后端。`services/indexing.py` 的 `IndexPlanner`
消费该结果，为 revision 物化稳定 ID、metadata 和 `IndexPlan`，说明哪些块向量化、
哪些块只存正文。后续分批写入由 `RevisionStager` 编排 Port，当前只推进到 ready。

### 7.4 Graph 属于知识提取与图索引

GraphRAG 的实体抽取、关系抽取、消歧、社区构建和图存储不能塞进 `ChunkerPort`。
调用外部 LLM 的部分使用 Port；纯本地算法使用 Strategy；流程由 Graph ingestion
Service 编排。

## 8. Retrieval 的拆分

不建立携带 `mode="hybrid"`、Milvus 表达式和 BM25 专有参数的万能 `SearchPort`。
目标结构是：

```text
RetrievalService
├── AsyncVectorSearchPort
├── AsyncKeywordSearchPort
├── FusionStrategy
└── RerankerPort
```

- ANN 与 BM25 是两条召回通道；
- Hybrid 是 Service 的编排方式；
- RRF/加权融合是纯计算 Strategy；
- Reranker 是外部模型 Port；
- 对 API 暴露的是稳定 `SearchRequest` / `SearchHit`，不是后端查询语法。

这些边界已在 M3 随真实 BM25 和过滤需求落地：两路 Search Port 位于
`ports/vector_store.py`，纯 RRF 位于 `engines/retrieval/fusion.py`。图中
`FusionStrategy` 表示算法职责，当前无需为单一 RRF 实现新增基类。

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
│   ├── knowledge_base.py            # 知识库持久化契约
│   ├── document_store.py            # revision manifest 与父块契约
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
│   │   ├── protocol.py              # Chunker / ChunkingStrategy
│   │   ├── fixed.py
│   │   ├── recursive.py
│   │   └── strategies/             # 已实现 markdown / page；语义断点仍待真实需求
│   ├── indexing/
│   │   └── hierarchy.py
│   └── retrieval/
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
│   │   ├── knowledge_base/
│   │   └── document_store/
│   └── task_execution/
│       └── arq.py
├── services/
│   ├── ingestion.py
│   ├── indexing.py                  # IndexPlanner / IndexPlan / RevisionStager
│   ├── revision_context.py          # 到 ready 的有界移交切片
│   ├── revision_recall.py           # 判活与有界召回
│   ├── pipeline.py
│   └── retrieval.py
├── api/
│   ├── routes/
│   └── schemas/
└── composition/
    └── bootstrap.py                  # 唯一跨层装配点
```

上图除标注的 Graph 等未来能力外均已落位；具体目录见 `docs/structure.md`。
结构归一化已按 `repository_structure_spec.md` 执行，重构前的内部路径已删除。

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

M3 规格已冻结并完成：Milvus 原生 BM25 位于 `AsyncKeywordSearchPort` 后，RRF 是 engines
内的纯计算 Strategy，Hybrid 由 RetrievalService 编排。`AsyncVectorStore` 已下沉 ports，
结构归一化阶段已删除内部旧路径；schema v2 不自动删除旧 collection。详细边界与成功标准见
`tasks/m3_spec.md`，实施清单见 `tasks/m3_todo.md`。

### P3 — M4：Chunking 与可选父子索引

M4 先完成带位置的 Chunk 契约、固定/递归策略、Pipeline/Task 单链路和 Markdown/Page
结构感知。独立 schema 决策门已经通过，`DocumentStore` 与三表迁移已在 M4-T7 完成；
`IndexPlan`、`HierarchyBuilder`、有界移交与 ready 续跑、revision-aware 检索切片均已实现。
DocumentStore 已有激活/回收原语，待完成的是生产激活/superseded、故障恢复、旧版本 GC
和完整读写接线。GraphRAG 仍不在 M4 范围。
详细顺序见 `tasks/m4_plan.md` 与 `tasks/m4_todo.md`。

### V2 首批 — 先校准现状，再分离配置合并

本批已按 `tasks/v2_refactoring_research.md` 完成：以当前代码、共享契约和 CI 校准文档，
将 `build_context` 中的配置合并提取到 `composition/pipeline_config.py`，保持现有覆盖
优先级、默认值和校验行为。实施验证与后续候选见 `tasks/v2_plan.md`。

### V2 全仓执行模型与上游能力重估

用户确认本轮一次完成双模式类及调用方迁移。实施不能只改名字：

- 先核查项目最低支持版本的上游能力；原生异步网络 API 优先，不能因旧实现用了线程而继续保留。
- 同步与异步实例各自拥有对应客户端，共享纯请求构造、解析和验证；公开方法同名。
- 纯计算与值对象保持单一实现。已有纯异步任务服务不制造同步副本；共享 Gate 继续保护同一预算。
- 线程只用于同步文件、DOCX、DNS/媒体准备等阻塞工作；取消和失败时先排干，再释放依赖。
- Port、适配器、组合根、Hook、Pipeline、测试替身、文档示例作为同一迁移闭环；旧 `a` 前缀入口直接删除。

证据矩阵、迁移说明见 [V2 执行模型重估](v2_async_reassessment.md)；批次验收见
[V2 实施记录](v2_plan.md)。本轮不改变持久化 schema，也不启用尚未完成的 M4 生产闭环。

## 11. Port 创建门槛

新增 Port 前必须同时检查：

- [ ] 有明确调用者与至少一个真实实现、一个测试实现；
- [ ] 抽象隔离了外部依赖或确有跨实现替换价值；
- [ ] 签名中没有供应商字段和无边界 `**kwargs`；
- [ ] 值对象、错误分类、资源所有权和关闭顺序已定义；
- [ ] 有实现无关的契约测试，并做过反向验证；
- [ ] 组合根是唯一选择具体实现的位置；
- [ ] 不破坏 core-only 安装和依赖方向；
- [ ] 若修改公开接口，已有明确迁移说明；V2 本轮按用户要求直接移除旧接口，不保留兼容别名。

不满足这些条件时，优先使用具体类、内部函数或 Strategy，不创建占位 Port。

## 12. 当前决策记录

| 决策 | 结论 |
|---|---|
| Loader 是否物理合并到一个层 | 是；Local/HTTP/S3/AutoLoader 统一在 `infrastructure/sources`，S3 SDK 惰性导入 |
| Loader 是否提供一个用户入口 | 是；M2 后增加 `comet_rag.loaders` 惰性门面 |
| 是否重构全部 Loader | 已完成；契约在 ports，实现统一在 infrastructure，公共入口为 `comet_rag.loaders` |
| MinerU 是否移入 `engines/documents` | 否；它是外部 `DocumentExtractorPort` 适配器 |
| DOCX 是否最终实现同一提取 Port | 是；M2 后 P1 已完成 |
| Chunker 是否统一做页面、父子块和 Graph | 否；原子 Chunker 只切字符串，页面/父子属于文档级 Strategy，写入属于 Planner，Graph 属于独立 ingestion |
| 是否定义 Search/Graph 全套 Port | M3 只定义 Vector/Keyword Search；Graph 仍由后续需求驱动 |
| `BaseParser` 是否删除 | 是；仅有一个实现且没有多态调用方，格式内部步骤不提升为项目级抽象 |
| Loader 是否提供统一公共入口 | 是；`ports/source.py` 是契约，`comet_rag.loaders` 是惰性公共门面 |
| Worker 是否收入 `tasks/` | 否；workers 是独立进程入口，必须与单进程会加载的通用任务框架隔离 |
| DOCX 是否改为垂直目录 | 已完成；专属 converter/parser/cleaner/extractor 位于 `engines/documents/docx` |
| 顶层 Parser/Converter 目录是否保留 | 否；格式专属代码归入 documents，ZIP 防护归入 documents/common |
| Provider 私有辅助模块是否整理 | 已完成；模型适配器位于 `infrastructure/models`，MinerU 位于 `infrastructure/extractors` |
| 完整语义分块是否是纯 Strategy | 否；Service 编排 EmbeddingPort，engines 只保留断点算法 |
| 当前下一项工作 | V2 各批结果和后续边界见 `tasks/v2_plan.md`；M4 生产激活、GC 与完整验收另按 M4 清单推进 |
