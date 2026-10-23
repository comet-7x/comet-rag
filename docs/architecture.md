# 架构

## 一句话

**一个通用的 RAG 框架，同时是库和参考服务。** 库那一半（`engines/`）只依赖
pydantic/httpx/lxml 一类的纯计算包；服务那一半在它之上加了任务调度、持久化
与 HTTP 接口。两半共用同一份解析与切分代码。

## 分层

```
    composition/    组合根：把配置装配成一整套资源  ┐
    api/            HTTP 入口（FastAPI）          ├─ 参考服务
    workers/        消费端进程（arq）             ┘
        ↓
    services/       用例编排（Pipeline、入库、检索、知识库）
    api/schemas/    HTTP 请求/响应 DTO
        ↓
    tasks/          通用任务框架、契约与参考实现
    infrastructure/ 来源、模型、提取、持久化与队列适配器
        ↓
    engines/        纯计算：文档处理、切分、融合、排程
        ↓
    ports/          契约（Protocol / ABC）与值对象         ┐
    core/           闸门、降级、日志、时间 —— 人人依赖       ├─ 零依赖地基
    config/  exceptions/                                    ┘
```

依赖方向**只能向下**。`tests/unit/test_layering.py` 用 AST 检查以下边界：

1. `engines/` 不得 import 任何基础设施包（redis / pymilvus / sqlalchemy /
   arq / fastapi …）。破了这条，用户为了跑一个 docx 解析器就得装一整套中间件，
   "库"这一半当场作废（spec A1）。
2. `engines/` 只能 import `engines` 与 `ports`（**白名单**）。
3. `services/` 不得 import `infrastructure/`；模型和向量存储适配器另有专门
   守卫。具体供应商只在组合根选择。
4. `ports/` 只能依赖标准库与自身，不能反向依赖使用者或适配器。
5. `core/` 不得 import 本项目任何其他包。它是人人依赖的零依赖内核，
   一旦回头依赖上层就出环。
6. 顶层包之间不得成环。环里的包无法被单独理解或单独拿走。
7. 单进程路径不得 import `workers.maintenance`（租约回收），理由见下文「崩溃恢复」。
8. 服务具体模型只能在 `composition/bootstrap.py` 构造，模型定义模块除外。
   绕开组合根会漏绑进程级闸门。
9. engines 的模块级并发默认值集中在 `engines/defaults.py`，避免调用处各自发明预算。

第 2 条原本是一份黑名单（禁止 api / workers / services）。黑名单只拦得住
已经想到的那几个包：后来新增的 `application/` 就从缝里溜了进去，`pipeline.py`
运行时 import 了它，而本文档白纸黑字写着 engines 在最底层。**文档说的规则
和守卫执行的规则不是同一条，中间差出的那个洞刚好够放一个错误。** 改成白名单
之后没有这个失效模式：新包默认被拒。

### 为什么 `ports/` 在最底层

契约必须比所有使用者都低，否则使用者就得向上依赖。`engines/` 需要说出
"我要一个 embedding 模型"；如果契约住在某个上层包里，engines 就只能反向依赖 ——
这正是上面第 2 条要防的事。`ports/` 不 import 本项目任何其他包，所以谁依赖
它都是向下。`ports/` 内部可以复用自身的契约和值对象；除此以外只依赖标准库。

值对象（`MediaResource`、`RerankDocument` …）也放在 `ports/`：它们是 Port
签名里出现的类型，也就是这套契约的词汇表。

`DocumentExtractorPort` 也遵循这条规则：它只接收 Loader 已经落地的受管本地文件，
返回 `ExtractedDocument`，不认识 URL、S3 凭据、MinerU backend 或 HTTP 响应。
所有结果在进入文档级 `ChunkingStrategy` 前由 `engines/documents/normalization/`
转换成 `NormalizedDocument`。规范化后的 Markdown 由
`engines/documents/markdown/` 识别标题 section，结构事实用 `DocumentBlock` 的字符
span 表示；`MarkdownSectionStrategy` 或 `PageChunkingStrategy` 再在每个硬边界内调用
原子 `Chunker`，不会重新猜标题或页面。
`AsyncMinerUDocumentExtractor` 是 `infrastructure/extractors/`
中的外部适配器，只有 `composition/` 能把它注册成 PDF Pipeline Hook。

### 组合根的配置合并

`composition/pipeline_config.py::resolve_pipeline_config` 只接收部署 `LimitsConfig`
和可选的调用方 `PipelineConfig`，计算有效配置，不创建资源。覆盖顺序是调用方显式值、
部署默认值、库默认值；即使显式值恰好等于库默认，也保留该覆盖。嵌套 DOCX 字段单独
判断是否显式设置，注入的模型对象保持身份，调用方配置不被修改。

合并后重新校验顶层和 DOCX 字段，拒绝构造后被赋成非法值的配置；同时保留
PipelineConfig 子类的约束和注入模型身份。

`await build_context(...)` 在创建资源之前重校验 APPConfig、合并 PipelineConfig、
检查实际所选后端的连接配置。它负责资源选择、共享闸门和装配；配置推导与资源
生命周期分别有可独立阅读和验证的入口。

### 组合根的资源所有权

初始化失败只回收本次成功创建的资源，按创建逆序等待清理完成，再传播启动错误。
注入对象此时仍归调用方；闸门绑定会修改注入对象，失败不恢复原闸门。
完整返回 Context 后，包含注入对象在内的资源交给 Context 托管。
模型、来源和外部提取器的同步/异步实例只创建各自模式的客户端；单个对象不再同时持有两套连接池。

正常关闭先排干执行器，再撤销 runner / Hook 引用，随后关闭 Loader、Extractor、
向量/文档存储和模型，最后关闭自建数据库。单项清理错误记录日志并继续，重复或并发
`close()` 共享同一次清理；调用方取消也要等待清理结束。runner 仅按对象身份解绑，
旧 Context 关闭不会移除新注册，且不会恢复已关闭的旧 runner。

API 和 worker 只在创建成功后发布 Context，关闭前撤销自己发布的引用。
当前 runner 注册仍是进程级单例；这项清理保护不代表支持同进程并行运行多套 Context。

## 执行模式与共享计算

需要同时面向同步和异步调用者的适配器、Pipeline、Loader 与 HookRegistry 分成
`Foo` / `AsyncFoo`。两边的方法同名：`run`、`embed_query`、`rank`、`extract`、
`load`、`close`；异步调用者显式等待。对应 Port 也分离，组合根只选择服务使用的
异步实现。`PipelineHooks` 与 `AsyncPipelineHooks` 是两个默认注册表实例，Context
从异步表 `fork()` 得到独立快照。

共享部分保留为同步计算：请求 DTO、输入规范化、响应解析、排序、分块器、DOCX Parser
与值对象不因调用方式增加一份 Async 算法。异步编排在必要处将同步文件读取、DNS、
ZIP/XML 解析交给 `engines.offload.run_sync`；取消时等待工作线程结束后才传播，防止
调用方提前释放输入资源。线程不能被强制终止，此处也不承诺 CPU 吞吐扩容。

上游有原生异步 API 时直接使用：HTTPX、OpenAI、aioboto3、SQLAlchemy/asyncpg、
ARQ/Redis 与 PyMilvus 均如此。`AsyncMilvusStore` 只持有 `AsyncMilvusClient`；
集合与索引管理也已使用原生异步接口。SDK 惰性连接，构造 Context 成功不等于 Milvus
可达；连接错误在首次等待的 RPC 暴露。

`TaskStore`、`TaskExecutor`、`DocumentStore`、`KnowledgeBaseRepository`、
`RevisionStager` 本来只有异步操作，保留领域类型名，方法移除 `a` 前缀；不创建同步
副本。Gate 刻意维持双模式：同步与异步实例必须竞争同一进程级预算，不能分成两份限额。
详见 [V2 异步能力复核](../tasks/v2_async_reassessment.md)。

## 核心抽象

每一个都配有一套**与实现无关的契约测试**（`tests/contracts/`）。
换实现时跑同一套契约，这是"换后端行为不变"这句承诺的唯一兑现手段 ——
不是靠文档，是靠契约断言。

| 抽象 | 内存实现 | 真实实现 | 契约源 |
|---|---|---|---|
| `TaskStore` | `InMemoryTaskStore` | `PostgresTaskStore` | [task_store.py](../tests/contracts/task_store.py) |
| `TaskExecutor` | `InProcessExecutor` | `ArqExecutor` | [task_executor.py](../tests/contracts/task_executor.py) |
| `AsyncVectorStore` | `AsyncInMemoryVectorStore` | `AsyncMilvusStore` | [vector_store.py](../tests/contracts/vector_store.py) |
| `AsyncKeywordSearchPort` | `AsyncInMemoryVectorStore` | `AsyncMilvusStore` | [keyword_search.py](../tests/contracts/keyword_search.py) |
| `DocumentStore` | `InMemoryDocumentStore` | `PostgresDocumentStore` | [document_store.py](../tests/contracts/document_store.py) |
| `KnowledgeBaseRepository` | `InMemoryKnowledgeBaseRepository` | `PostgresKnowledgeBaseRepository` | [knowledge_base.py](../tests/contracts/knowledge_base.py) |

表中链接指向共享契约基类；参数化后的用例数量应以具体实现的 `pytest --collect-only`
结果为准，不在文档维护另一份计数。

向量与文档存储负责 metadata 的所有权：写入调用完成后，修改输入不影响已存数据；
每次读取返回独立快照，修改嵌套字典或列表不影响后续读取及其他结果。调用尚未完成时，
调用方不应并发修改输入。内存实现必须显式复制，不能依赖真实后端的序列化边界。
内存批量写入先完成校验和全部快照，再发布，避免复制失败留下半批数据。
`RevisionDraft` / `StoredParent` 构造时也隔离 metadata；只有最外层只读，内部仍保持
JSON 风格的 dict/list，不能把 frozen dataclass 当作递归不可变对象。

`DocumentStore` 只保存 revision manifest 与不参与召回的父块正文。PostgreSQL 的 active
pointer 是版本可见性的唯一真相；清理 retired/abandoned revision 时保留 `purged`
manifest 墓碑，避免旧任务在 GC 后重新申请更高 generation 并覆盖新版本。Milvus 子向量
及读写编排不属于 Store 自身职责。

M4 当前已有 `HierarchyBuilder`（`engines/indexing/`）、`IndexPlanner`/`RevisionStager`
（`services/indexing.py`）、有界 Task context 移交与 ready 续跑切片，以及 revision-aware
召回和父块回填。Store 的激活与回收原语已实现；Stager 只写到 ready，生产 `IngestRunner`
尚未接入完整的激活/superseded、响应丢失恢复和旧版本 GC。读侧可显式配置或注入
DocumentStore，默认关闭。M4 的完整完成标准见 `tasks/m4_todo.md`，不能由切片通过推定。

检索侧刻意把完整存储生命周期与只读能力分开：`AsyncVectorSearchPort` 只暴露 dense
召回，`AsyncKeywordSearchPort` 只暴露关键词召回。`RetrievalService` 按请求选择一路或
并发调用两路，hybrid 的原始分数不直接相加，而是在 `engines/retrieval/` 用纯 RRF
按名次融合。这样 Milvus 的表达式、BM25 index 与 consistency level 都不会穿透 Port。

两路各自最多返回 `fetch_k` 条，因此去重前最多有 `2 × fetch_k`（当前上限 1000）
个候选进入 RRF；去重后的全部候选会送给 reranker。调大 `fetch_k` 会同时增加 Milvus
返回量和重排成本，不是免费的精度旋钮。关键词契约只保证后端行为语义一致：内存实现
的轻量 tokenizer 与 Milvus chinese analyzer 不承诺相同分词或相同 BM25 分数。

hybrid 单路出现可恢复故障时只降级该路；schema、维度、知识库与请求参数错误仍直接
失败；revision 判活失败也必须拒绝整次查询。不可降级的通道失败会取消并排干另一通道，
不会等待其正常请求完成；调用方取消或重复取消同样等待子任务清理完毕。两路均出现
可恢复故障时返回 503。

重排位于融合之后，服务在降级边界内校验返回结构、完整整数索引和有限分数。失败时
返回原召回/融合候选并留下结构化诊断，取消则传播。输入 metadata 使用独立快照，
重排器修改输入后失败也不会污染原候选；有效结果按索引匹配原候选，以 `replace`
仅更新分数，保留各路分数和排名。

适配器基类的同步/异步 `score()` 还会在直接返回前检查全部分数，非法值抛 ValueError；
`rank()` 复用该入口，在排序与截断前完成检查。这不替代服务校验，因为独立实现的 Port
可以绕过基类。供应商输入转换留在适配器，候选排列和降级仍归服务负责。

父块回填分开处理批量读取与纯结果物化。查询 parent_id 可以预先去重，但必须逐项验证
父块的 ID、KB、source 和 revision 后，才去重成功回填的父块；无法回填的 child 各自保留。
完整排序候选处理完毕后才截取 top-k，避免重复父块耗尽名额或坏引用抢占合法结果。

### 为什么 store 与 executor 是分开的

`TaskStore` 回答"这个任务现在怎么样了"（持久、可查询）；
`TaskExecutor` 回答"接下来谁该干活"（短暂、消费即消失）。

合成一个的话，要么队列变得不可查询，要么数据库被当成高频争抢的队列用。
更直接的理由：`spawn(task, coro)` 收的是协程对象，Redis 里没地方放协程 ——
一旦把它写进"换实现时签名不变"的接口，这句承诺就是假的。

拆开之后，**队列里只放 `task_id`**，任务数据一律回库读。于是「重试」
「崩溃恢复」「断点续跑」「跨 worker 移交」全都退化成同一个动作 `submit(task_id)`。

## 任务的一生

```
PENDING ──► RUNNING ──┬─► SUCCEEDED
   ▲         │        ├─► FAILED ──► PENDING（人工 retry）
   │         │        └─► CANCELLING ──► CANCELLED
   └─────────┴ 可重排队（可重试失败 / 租约回收 / 换道移交）
```

状态只能经 `TaskStore.transition()` 改，守卫是一张手写的迁移表
（`tasks/states.py`）。乐观锁 CAS 保证并发写不丢更新；
调用方**没传** `expected_version` 时冲突会自动重读重来，
传了才把冲突抛给它 —— 这个区分是跨进程部署逼出来的（见 `store.py::_cas`）。

## 两种部署形态

### 单进程（开发、小规模）

```
comet-rag serve      # API + InProcessExecutor，任务在本进程的协程里跑
```

不需要 Redis，不需要 worker。分道信息被忽略，整条流水线一口气跑完。

### 跨进程（生产）

```
comet-rag serve                 # 只投递，不执行
comet-rag worker preprocessor   # CPU 密集：解析 / 清洗 / 分块
comet-rag worker embedder       # IO 密集：向量化 + 写库
```

**worker 按负载特征分，不按业务名词分。** 判据只有一条：这类活该怎么扩容。

| | 负载 | 扩容方式 | 默认并发 |
|---|---|---|---|
| preprocessor | CPU 密集（GIL 挡着） | **加进程**（副本 ≈ 核数） | 2 |
| embedder | IO 密集（等回包） | **加并发**（调 max_jobs） | 32 |

反过来会真的更慢：给 embedder 加进程会把对模型服务的连接数乘以进程数，
请求在服务端排队，而每个进程只看到"上游变慢了"，看不出是自己造成的。

流水线在 `chunking` 与 `indexing` 之间**移交**：任务退回队列、带上
`resume_stage`、投到另一条道。移交复用的正是断点续跑那套机制 ——
两者在实现上是同一件事，区别只在原因。

## 崩溃恢复

worker 被 `kill -9` 时，库里那条任务会永远停在 RUNNING：没人推进、没人报错。
所以跨进程部署必须挂租约回收（`workers/maintenance.py`）：心跳超过 lease
（90s，是心跳间隔的 9 倍）就判定 worker 已死，退回队列。

**单进程模式绝不能挂** —— 那时协程还活在本进程里，回收会造出"一份任务两个
执行者"。保证方式不是开关（会被配错），而是结构：`sweep_cron` 只在 `workers/`
下注册，单进程部署根本不加载那个包，且有 AST 守卫盯着。

租约判死做不到准确（"没心跳"和"死了"分不清），所以执行器侧还有一道**围栏**：
写终态前确认任务还归自己管，被接管了就一个字都不写。
**lease 负责少误判，围栏负责误判了也不出错**，两者缺一不可。

## 资源治理

```
                        ┌── 模型闸门（embedding 与 rerank 共用）
POST /ingest ─► 积压上限 ─┼── Loader 闸门（文件描述符与外部连接）
                        ├── MinerU 闸门（外部文档解析服务）
                        └── 降级：关 rerank → 降 top_k → 拒新任务
```

**闸门同时管同步与异步。** 预算是 `threading` 原语，两侧共用一份计数 ——
`asyncio.Semaphore` 只有协程侧能拿，同步路径必然漏出去（实测配置写 4、
真实并发 16）。等待不占线程：一条 FIFO 队列里同时装同步等待者（Event）与
异步等待者（Future），取消只是把自己摘掉。

**闸门按资源分，不按模块分。** embedding 与 rerank 共用一个（抢的是同一块
GPU）；加载另配一个（护本机文件描述符与对外连接）；MinerU 再单独一个（护外部
解析服务）。混用会让"调大加载并发"意外挤掉模型或 PDF 解析名额。

**闸门必须是进程级的。** 曾经是"每次调用建一个信号量"，于是 32 个任务各开
4 路扇出 = 对模型服务 128 路并发，而配置写的是 4，监控上完全看不出来。

闸门做在适配器基类的**模板方法**里：同步/异步类各自的 `embed` / `embed_media` / `embed_batch`
都标了 `@final`，子类只能实现下面那层 `_embed` / `_embed_media` /
`_embed_batch`。于是"绕过闸门"在类型层面就没有写法。

之所以不用"包一层 wrapper 再让大家都走 wrapper"：那只是**约定**，谁直接调
底层都能绕过去，而且不报错 —— 闸门是静默失效型的保护，约定挡不住它。

`services/` 只依赖 Port 和共享值对象，不再直接依赖 `infrastructure.models`。
具体的 Qwen/OpenAI 类只在组合根中选择并装配；这让模型实现可以替换，而查询、
入库和重排用例不需要认识供应商请求字段。

所有队列都有界：闸门外的等待席位、等待时长、待执行任务数，任一越界都
**明确拒绝**（HTTP 429），不静默排队。

## 相关文档

- [目录结构与核心流程](structure.md) —— 东西放在哪、一次请求怎么走完（含依赖图与流程图）
- [部署](deployment.md) —— 两条路径的具体命令与配置
- [性能基线](benchmark.md) —— 数字的定位与采集方式
- [Pipeline 用法](pipeline_usage.md) —— 只当库用时看这个
- [docx 解析内幕](docx_parser_internals.md)
- [MinerU 集成](mineru_integration.md) —— M2
