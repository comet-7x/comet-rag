# 版本说明

## Unreleased — V2 执行模式与职责重构

V2 优先清楚的资源所有权与调用边界。本轮是 Python API 的破坏性变更，所有仓内
调用与示例一并迁移，不保留旧 `a*` 方法或导入别名。数据库和向量数据格式不因这次
同步/异步拆分改变；下文已有的 M3/M4 schema 迁移要求仍独立成立。

### 查询共享 deadline

检索从服务入口起默认限制为 30 秒工作预算，可通过 `retrieval.timeout_seconds`
调整（直接使用 RetrievalService 时为构造参数）。排队、召回及判活、重排、父块读取
共用剩余时间；无成功召回结果时新增 HTTP 504，hybrid 可保留已成功通道，重排/父块
超时退回已有候选并返回 deadline_exceeded 诊断。调用方取消继续传播，资源排干可能
超过预算。详见[查询 deadline](query_deadline.md)。

### 入库线程取消与清理

IngestRunner 的正规化与分块改用已有 run_sync，取消后等待同步工作结束再传播，
防止任务先落终态、临时文件先删除、线程仍在运行。临时文件清理也通过同一线程桥执行，
避免阻塞事件循环。取消后的线程异常会被消费，迟到结果不写入 Task context。
关停可能因此等待更久；没有修改正常解析、分块、metadata 或索引格式。

### 同步与异步 API 分离

| 旧用法 | V2 用法 |
|---|---|
| 同一模型的 `embed/aembed`、`rank/arank`、`describe/adescribe` | `Foo` / `AsyncFoo` 两个类型，同名 `embed`、`rank`、`describe` |
| 一个实例注入 `sync_client=` 和 `async_client=` | 每实例仅注入本模式的 `client=`，外部客户端仍归调用方 |
| `Pipeline.arun/astream_run/abatch_run` | `AsyncPipeline.run/stream_run/batch_run`；流式用 `async for` |
| `PipelineHooks.aextractor` | `AsyncPipelineHooks.extractor` 注册异步函数；同步函数显式用 `sync_extractor` |
| `PipelineHooks` 作为类使用 | `HookRegistry` / `AsyncHookRegistry` 为类型；两个默认 Hooks 是独立实例 |
| `aembed_documents` | 异步导入 `engines.embedding.async_batch.embed_documents`；同步导入 `engines.embedding.batch.embed_documents` |
| Loader 的 `aload/abatch_load/acleanup` | `AsyncFooLoader.load/batch_load/cleanup`；同步类只提供同步操作 |
| `BaseVectorStore`、`MilvusStore`、`InMemoryVectorStore` | `AsyncVectorStore`、`AsyncMilvusStore`、`AsyncInMemoryVectorStore`，方法使用 `search/upsert/...` |
| `await abuild_context(...)`、`context.aclose()` | `await build_context(...)`、`await context.close()` |
| 纯异步领域接口的 `a*` 方法 | `DocumentStore`、`KnowledgeBaseRepository`、`RevisionStager` 等保留类型名，使用同名无前缀异步方法 |

同步模型 `close()` 直接完成；异步模型 `close()` 必须等待。Pipeline 仅关闭自己创建
的默认 Loader，不接管注入模型；服务 Context 按组合根所有权规则统一管理资源。
同步/异步 DTO 与纯计算实现继续共享。`DocxConverter`、`DocxParser`、分块算法没有
无 I/O 的 Async 副本；异步提取器负责必要的线程卸载。

### 任务 runner 注册表改为显式实例

移除 `tasks.register/get_runner/registered_kinds` 与 `tasks.runner.unregister` 的全局入口。
注册与查找改为 `RunnerRegistry.register/get/kinds/unregister`。每个执行器默认持有独立的
`executor.runners`；应用装配后从 `context.runners` 访问同一份绑定。

```python
from comet_rag.tasks import Done, InMemoryTaskStore, InProcessExecutor, RunnerRegistry, TaskContext

runners = RunnerRegistry()

@runners.register("example")
async def example(ctx: TaskContext) -> Done:
    return Done(result="ok")

executor = InProcessExecutor(InMemoryTaskStore(), runners=runners)
```

`ArqExecutor` 同样接受 `runners=`。`build_context` 也接受显式注册表；注入自定义
TaskExecutor 时必须同时提供它，自带执行器与显式注册表不一致会在创建资源前报错。
跨进程消费者必须各自装配 runner，不能靠生产端
注册替代消费端注册；队列仍只放 task_id。`register_ingest_runner` 现在先接收注册表，
默认拒绝重复注册，替换自己应用的绑定时显式传 `replace=True`。
Context 先排干执行器，再清空自己的注册表和关闭资源；独立 Context 的启动失败、
取消与关停不会撤销另一个 Context 的绑定。不要把一个执行器或持有 I/O 资源的
registry 注入多个 Context。TaskExecutor 的抽象方法与任务数据格式没有改变。

### 原生异步与取消

PyMilvus 2.6.17 已支持本项目需要的异步管理 API；移除了同步管理客户端与其线程
包装，只保留 `AsyncMilvusClient`。该 SDK 惰性连接，连通性失败从构造阶段移动到
首次被等待的 RPC。启动成功和进程健康探针不再能被误读为 Milvus 连通性证明。

Milvus 建索引异常不再自动删除集合：服务端可能已完成操作，其他实例也可能已写入。
失败和取消后可再次显式调用 `ensure_collection`，补齐完全兼容 schema 中确实缺失的
索引。旧 schema 和已有不兼容索引仍报错，不自动替换。此修复不改变字段或数据格式。

HTTPX、OpenAI 与 aioboto3 的网络操作保持原生异步。必须调用同步文件/DNS/解析代码
时才在线程中执行；取消等待线程结束，避免提前删除输入文件。无法强制中断线程，
因此慢磁盘、DNS 或同步扩展也会延长取消完成时间。执行模式与本地上游版本证据见
[V2 异步能力复核](../tasks/v2_async_reassessment.md)。

### Python 导入路径收敛

项目仍处于 `0.1.0`，目录归一化分支尚未发布，因此本次直接删除只做转发的历史内部
目录。Loader 与 Pipeline 的稳定入口分别是 `comet_rag.loaders` 和
`comet_rag.pipeline`；具体实现只存在于 `infrastructure/sources`、
`infrastructure/persistence`、`infrastructure/models`、`infrastructure/extractors`
及 `engines/documents` 等规范目录。HTTP DTO 位于 `comet_rag.api.schemas`。

### 文档 Hook 返回值变更

`PipelineHooks.extractor()` 与 `AsyncPipelineHooks.extractor()` 注册的函数现在必须返回
`ExtractedDocument`，不再直接返回字符串。Pipeline 会统一转换为
`NormalizedDocument` 后再交给文档级 Chunking Strategy；Strategy 把 Markdown 交给
原子 Chunker，避免 DOCX、MinerU 与后续 PDF/OCR 各自维护一套跨格式空白和编码规则。
自定义 Hook 可按以下方式迁移：

```python
return ExtractedDocument(markdown=text, metadata={"provider": "custom"})
```

### Chunker API 收敛

原子分块器现在直接接收 `str`，当前只有 `FixedSizeChunker` 和 `RecursiveChunker`。
`BaseChunker`、`RecursiveCharacterTextSplitter` 及按格式/代码语言命名的参数型类已经
删除；对应差异改用 `ChunkProfile`、`language_profile()` 与 `code_profile()` 表达。
所有 separator 常量均为不可变元组；来源、知识库与索引关系的 metadata 合并规则位于
`services.chunking`，不再作为原子 Chunker 的公共 API。

`MarkdownDocumentNormalizer` 现在生成引用规范 Markdown 的 section blocks；内置
`MarkdownSectionStrategy` 保留标题路径，`PageChunkingStrategy` 只接受带真实页码的
page blocks。两个策略都复用原子 Chunker，输出位置仍指向整篇规范 Markdown。

MinerU 现在可从经过全文一致性校验的 legacy content list 生成页事实；无法无损重建时
保守回退 section。DocumentBlock 同时通过严格 JSON payload 跨 Task 阶段保存，不再因
worker 交接丢失页面或标题结构。

### 父子索引设计门

M4-T6 已冻结父子索引与 revision 的接口、三表 PostgreSQL schema、Milvus metadata、
有界判活、故障恢复和回滚顺序，详见 `tasks/m4_t6_design.md`。T6 当时只有设计变更；
持久化决策门随后已确认并进入 T7。

M4-T7 已在决策门确认后实现 `DocumentStore`：内存/PostgreSQL 运行同一套契约，Alembic
新增 documents、document_revisions 和 document_parents。GC 保留 purged manifest 防止
旧任务复活；本阶段仍未把 revision 写入生产入库或检索路径。

server 依赖使用 `sqlalchemy[asyncio]`，确保异步 SQLAlchemy engine 所需的 greenlet 在
支持的平台上随 extra 一起安装。

### 配置变更

`infrastructure_config.vector_database.collection_name` 已移除，改为：

```yaml
infrastructure_config:
  vector_database:
    endpoint: http://localhost:19530
    database_name: your_database       # 必填，不会回落到 default
    collection_prefix: comet
    replica_number: 1
```

应用与集成测试必须使用明确隔离的 database。测试代码只允许访问
`zhihao_test_database`，且不会自行创建或删除 database。

### Milvus schema v2 是破坏性变更

M1/M2 创建的旧 collection 没有 chinese analyzer、BM25 function 或 sparse index，
不能原地变成 M3 schema v2。所有读写入口都会校验 schema：检索请求返回 HTTP 409，
入库任务在 indexing 阶段永久失败。服务不会自动删除、迁移或覆盖已有 collection。

升级步骤：

1. 备份或导出需要保留的原始文档与 collection 数据。
2. 在配置中显式填写目标 `database_name` 和 `collection_prefix`，确认没有指向生产外的库。
3. 停止写入；通过 `collection_name_for(kb_id, prefix=...)` 核对每个知识库对应的
   collection 名。
4. 仅在确认原始数据可重建后，由操作者显式删除旧 collection。
5. 重新提交全部数据源，使用当前 embedding 模型整库向量化；不要把新旧模型的向量
   混在一个知识库中。
6. 验证入库任务成功，再分别执行 dense、keyword、hybrid 查询后恢复流量。

建议先在一个可重建的知识库上做 canary。没有原始数据或备份时不要删除 collection。
