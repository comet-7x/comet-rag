# 版本说明

## Unreleased — V2 执行模式与职责重构

新增仓库级 [检索评测工具](retrieval_evaluation.md)：固定生成语料/标签、严格评分、12 组
离线回归，以及只访问模型服务的实测入口。工具不进入 wheel，不改变服务 API/Store
契约或默认检索策略；小型合成语料分数不作为生产质量门槛。

V2 优先清楚的资源所有权与调用边界。本轮是 Python API 的破坏性变更，所有仓内
调用与示例一并迁移，不保留旧 `a*` 方法或导入别名。数据库和向量数据格式不因这次
同步/异步拆分改变；下文已有的 M3/M4 schema 迁移要求仍独立成立。

### Worker 原生信号排干与独立进程恢复

WorkerProfile 增加 job_completion_wait，默认 30 秒并传给 ARQ 原生设置。SIGTERM/SIGINT
先停止拉取新任务，允许在途工作收尾，窗口耗尽仍沿用 CANCELLED 语义；已提交 active
不回滚，取消任务不能通过 retry 重开。程序化画像允许非负整数，0 保留立即取消行为。
没有新增 YAML 配置、任务状态或重试框架。部署需另留取消/线程排干和关闭连接的时间，
30 秒不是进程退出硬上界。详见[关停与恢复](deployment.md#worker-关停与恢复)。

新增真实独立 CPU/IO 进程的 SIGTERM、激活前后 SIGKILL 与超时取消演练；恢复使用原
阶段、租约和 Store 原语，原文件移除后不重算 ready/已提交版本。单测将认领竞态和
闸门排队改为状态/事件协调，保留对应缺陷反向验证；并更新并发诊断中的旧异步接口提示。

### 新命名空间配置预检与演练

新增 `comet-rag revision-preflight`，显式读取旧端及新 API/CPU/IO 四份配置，核验暂停、
持久化组合、三方 writer 绑定和不同逻辑存储命名空间。纯配置检查不创建 Context、
连接中间件或授权切流；失败退出 2，不输出配置/异常正文。CLI 使用默认分块计划，
程序化预检须同时提供三方 Pipeline 覆盖。详见[迁移与回退](revision_migration.md)。
独立测试命名空间补齐受控重建、激活前失败/响应丢失、切读与切换后增量回退；实际
生产资料、权限、模型质量、跨主机恢复及全副本停投仍需目标部署验收。未改变默认开关、
持久化字段、Store 接口或外部依赖。

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

### 实验性 revision 激活恢复

新增 `services.revision_activation.RevisionActivator`，只依赖 DocumentStore 现有原语；
`RevisionTaskStages` 现在显式注入 `document_store=` 并独立执行 `activate(ctx)`，替代
实验切片原来的 `activator=` 参数，以便恢复时复验 manifest。事务失败交给
调用方重试同一 manifest；提交后响应丢失返回 already_active，旧任务返回 superseded，
不覆盖较新的 active。准备、激活与 GC 各有独立重试边界，激活阶段不重新 embedding。
返回值是本次尝试的快照，不保证返回后仍为当前版本。未新增 schema、Port 或生产开关；
生产 IngestRunner 接线仍待完成。

### 实验性 revision GC

新增 `services.revision_gc.RevisionGarbageCollector`：`collect_superseded(outcome)` 精确
登记并清理被取代版本，失败原样抛出供独立阶段重试；`sweep(limit=32)` 有界扫描已登记
的 retired/abandoned，返回成功/失败 key 并记录失败。原子拒绝 active 早于向量 IO，
向量删除和零残留核验早于父块清理及 purged 墓碑。缺失 collection 视为空，连接异常
不能视为空；不会清理 legacy 或其他 source/KB。
新增 `collect_inactive(key, checkpoint=...)` 用于从持久化身份精确恢复和补偿 purged 的
迟到写入。Task 阶段注入 `garbage_collector=` 后可独立执行 `cleanup(ctx)`；context 保存
activating/active/cleanup_pending/cleanup_complete，complete 重放仍会删除并核验。
未新增 Store 方法、Task 字段、schema、生产开关或定时任务。旧任务没有发布记录时可
从原 ready/active manifest 恢复。生产接线仍需旧写排干/对账和意图保留策略；本服务
不替代任务租约或跨存储事务。协议与边界见 [架构](architecture.md#revision-任务恢复协议)。

### TaskStore 游标与受保护删除

第十八批增加 `TaskCursor` 和 `list_tasks_before(kind=..., before=..., limit=...)`。
排序为 created_at/task_id 倒序，before 排他；原 offset 接口保留供普通查询，不用于持续
对账。自定义 TaskStore 实现需支持 `_query(..., before=...)`，并在
`_remove(task_id, expected_version=...)` 原子比较版本，不符抛 VersionConflict。
public delete 新增 guard/expected_version；force 不绕过 guard。未显式传版本时冲突
重新读取并复验，显式版本冲突原样传播。没有降级成先查后删的兼容兜底。

应用 TaskService 新增可选 delete_guard；组合根已接入 revision 保留策略，普通任务
删除不变。新增独立 RevisionReconciler，从带所有权围栏的 TaskContext 有界续页；不
新增后台循环、cron 或生产写开关。不支持依赖 Task TTL 丢弃 GC 身份，详见
[持续对账设计](../tasks/v2_reconciliation_design.md)。

### 准备意图与显式维护调度

第十九批在 `RevisionTaskStages.prepare` 的首次 DocumentStore 写入前，保存有界
`revision_prepare` 计划身份；完整 handoff 确认后才清空意图/原文。新记录的移交前取消
可精确回收，失败重试必须匹配原计划；旧缺载荷记录仍保留并告警。未改变 Task 字段、
表或向量 schema，原 handoff/publication 的版本 1 格式继续可读。

新增默认关闭的 `revision_reconciliation` 配置，明确来源 kind 后才能开启；自动模式
限定 ARQ/PostgreSQL/Milvus，组合根创建配套存储，不接受不透明存储注入。IO worker
每分钟处理一页，维护游标幂等保留，失败续页复用既有租约/状态机；不改变生产 ingest。
启用维护不启用 revision 检索，单进程不启动维护循环。`ArqExecutor.reconcile_pending`
新增可选 `exclude_kinds`，worker 用它排除维护游标，默认调用方行为不变。
TaskService 新增可选 `pending_kinds` 回调；应用按当前注册的业务 kind 在查询时统计
积压，维护游标不挤占业务准入，动态注册仍生效。直接使用通用 TaskService 且不传
该回调时，继续统计所有 PENDING，原库行为不变。

`GET /admin/limits` 新增 `revision_reconciliation` 状态，默认 null；开启后可读最近页、
错误码和超过 180 秒无页确认的 overdue。完整调度/告警、取消和存储命名空间边界见
[设计](../tasks/v2_reconciliation_design.md)。没有新增告警服务或对外通知。

### 历史缺载荷诊断与入库准入暂停

第二十批把缺少准备意图和 handoff 的终态记录从 `deferred` 分离为
`ReconciliationDisposition.MISSING_PAYLOAD`，页报告新增 `missing_payload` ID 列表。
损坏的非空载荷继续进入 failed；不调用猜测的 reserve/GC，不修改源任务。维护最近页
持久化该字段，并新增错误日志事件 `revision_reconciliation_recovery_required`。
旧维护 result 可能没有新字段，调用方按空列表兼容；最近页不是全历史审计。

新增 `ingest_policy.accept_new_tasks`，严格布尔，默认 true。false 时 API 新入库及手动
`ingest` 重试返回 HTTP 503，早于来源 IO/任务写入；`from_scratch` 也不绕过。已受理任务
继续消费，读取/取消/其他 kind 不受此开关影响。`/admin/limits` 新增
`accept_new_ingest_tasks`。这是 API 准入控制，不改 TaskService、Store 或执行器契约，
不能当作远端写入围栏。变更配置需重新构建 Context/部署所有 API 实例。

第二十批确定新命名空间重建后切换；后续生产接线见下文，实际用户资料迁移仍待完成。
证据分类、配置范围与后续验收见 [历史恢复与迁移边界](revision_migration.md)。

### 共享提取与有界恢复

入库的取源/解析/正规化提为 `DocumentExtraction`，旧 IngestRunner 和新路径共用，
取消后仍排干同步工作再清理临时输入。整份提取 context 在写入前也执行字节预算。
`get_revision_key` 改为有界 CAS；`load_recovery_draft` 与 RevisionReconciler 新增可选
预算参数，由组合根将 writer、恢复、维护统一到 `limits.task_chunk_context_max_bytes`。
默认仍为 32 MiB；没有修改解析内容、DOCX 快照、Store 抽象接口或持久化 schema。

### 可选生产 revision 入库

新增默认关闭的 `revision_ingestion`：显式 KB 清单、可选 parent_chunk_size。
启用要求 ARQ/PostgreSQL/Milvus、配套 revision-aware 读侧，以及来源 kind 为
`revision-ingest` 的持续对账。组合根只注册新 writer，不同时注册 legacy ingest；
发现同一 TaskStore 已有 legacy ingest 记录会拒绝启动。配置与实际新命名空间隔离
仍由部署方核验，不把空计数当作证明。

POST /ingest 按配置选择 kind；新模式保存配置绑定摘要，消费者在 IO 前复验存储、模型、
Pipeline 设置和 KB 范围。手动重试同样复验，错配返回 409；暂停开关覆盖两种 kind。
配置绑定不保存凭据，凭据轮换不改变身份；同一批 pending 任务需要保持计划配置一致。
/admin/limits 新增 ingest_kind。API 不接受客户端控制内部绑定值。

新 RevisionIngestRunner 复用 StagePipeline/RevisionTaskStages：CPU extracting/preparing，
IO indexing/activating/cleanup；只有 ready 不能写成功。激活确认丢失从同一版本恢复，
持久化发布后即使 from_scratch 也不重新取源/embedding。superseded 清理自己后正常完成。
active 的 cleanup 现在按 manifest.replaces_revision_id 精确清理前任，失败重试 cleanup；
这也作用于直接使用 RevisionTaskStages 的调用方。提交后的新 active 保持可见，Task
仍可能因后续 GC 失败处于重试/失败状态，不能据此回滚 active。

网络/超时/限流继续使用现有瞬时错误分类；其他错误落 FAILED 后可人工重试，并不保证
每种 SDK 异常都会自动重试。实际资料迁移、全环境部署切换/回退和完整 H/I 仍待验收。
配置和边界见 [生产 revision 配置](revision_migration.md#生产-revision-runner-配置)。

### Milvus 完整性读取（既有行为）

Milvus count 与删除 ID 选择固定使用 Strong，dense/keyword 检索仍遵循配置。
这避免把跨客户端旧快照作为完整性判断依据，可能增加读取延迟，但不改过滤语义或数据格式。

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
