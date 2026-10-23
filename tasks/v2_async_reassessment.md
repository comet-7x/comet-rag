# V2：上游能力与执行模式复核

复核日期：2026-10-22。证据来自本仓当前 `.venv` 的已安装源码与
`importlib.metadata.version()`；这是本次实现所依据的版本快照，不代表上游最新版本。
源码路径在下表中相对 `.venv/lib/python3.12/site-packages/`。

## 本次纠正的两条判断规则

**先核查上游，再决定桥接。** “旧适配器用了线程”不能证明上游没有异步能力。
本次逐项查看本地 SDK 的实际方法、初始化与关闭流程，再检查项目调用点。PyMilvus 的
集合/索引管理已有原生异步 API，继续保留同步管理客户端和 `to_thread` 没有依据。
HTTPX、OpenAI、aioboto3 等原有异步链路应继续直接等待原生客户端。

**执行模型由类型表达。** 同一实例同时承载同步和异步网络操作，会混淆客户端所有权、
关闭时机与接口契约。本次将双模式对象分为 `Foo` / `AsyncFoo`，方法同名，不再用
`a` 前缀在一个类内分叉。共享请求构造、媒体校验、结果转换仍只有一份实现。
纯异步领域对象保留原类型名；纯计算和数据对象不机械创建 Async 副本。

## 本地上游版本与能力矩阵

| 上游与本地版本 | 已核实的能力 / 源码证据 | 项目落点与本次结论 |
|---|---|---|
| PyMilvus **2.6.17** | `pymilvus/milvus_client/async_milvus_client.py` 的 `AsyncMilvusClient` 原生提供 `has_collection`、`describe_collection`、`create_collection`、`create_index`、`list_indexes`、`describe_index`、`load_collection`、`search`、`query`、`insert`、`upsert`、`delete`、`close`；调用下层 async gRPC handler | `AsyncMilvusStore` 只保留一个 `AsyncMilvusClient`。删除同步管理客户端及网络 RPC 的线程桥接。Schema / IndexParams 构造是本地计算，可继续同步调用。 |
| HTTPX **0.28.1** | `httpx/_client.py` 独立的 `Client` / `AsyncClient`，后者的 `send`、流式读取、`aclose` 为异步 | Qwen、URL Loader、MinerU 分为同步/异步适配器，每实例仅创建本模式连接池；异步网络直接使用 AsyncClient，不将同步请求包进线程。上游自身 `aclose` 的名字不改。 |
| OpenAI Python **2.30.0** | `openai/_client.py` 的 `OpenAI` / `AsyncOpenAI`，异步 resources 直接 await SDK 的请求层 | Embedding / Vision 各有同步与 Async 类型。公共注入参数统一 `client=`，外部客户端不被适配器关闭；内部只创建一个匹配模式的 SDK client。 |
| aioboto3 **15.5.0**、aiobotocore **2.25.1** | `aioboto3/session.py` 的 Session 使用 AioSession；`aiobotocore/session.py` 的 `_create_client`、客户端上下文与网络请求可等待 | `AsyncS3Loader` 复用 aioboto3 异步客户端与流读取；没有将 boto3 网络请求放进线程。临时文件写入/校验等阻塞工作单独卸载。 |
| boto3 **1.40.61** | 同步 `client("s3")`、同步 Body 读取 | `S3Loader` 单独供同步调用者使用。它不再顺带构造 aioboto3 客户端；是否有异步上游已由上一行明确。 |
| SQLAlchemy **2.0.48** | `sqlalchemy/ext/asyncio/session.py` 的 `AsyncSession.execute/close`，`engine.py` 的 `create_async_engine`；内部通过 greenlet 衔接同步 ORM 核心与异步驱动 | `infrastructure/persistence/sql/session.py` 已使用异步 engine/sessionmaker。继续使用其官方异步 API，不在应用层另包线程；内部 greenlet 不是同步数据库网络调用的线程替代。 |
| asyncpg **0.31.0** | `asyncpg/connection.py` 的 `connect`、`execute`、`fetch`、`fetchrow` 为 coroutine | PostgreSQL 路径已有原生异步驱动，不需要新增 Async 包装类或 `to_thread`。 |
| ARQ **0.28.0** | `arq/connections.py` 的 `create_pool` 是 async；`ArqRedis` 继承 Redis asyncio 客户端 | 分布式执行器继续等待原生队列操作。TaskStore / TaskExecutor 只有异步契约，保持领域名称。 |
| redis-py **5.3.1** | `redis/asyncio/client.py` 的 Redis `execute_command`、pipeline `execute` 和 `aclose` 为 async | 队列连接保持异步；上游关闭 API 与本项目同名 `close` 边界可分别使用，不添加同步代理。 |
| python-docx **1.2.0** | `docx/api.py` 的 `Document` 与对象模型是同步 ZIP/XML 加载与遍历 | `DocxConverter` / `DocxParser` 只保留一份同步实现。`AsyncDocxDocumentExtractor` 在需要时卸载加载与解析，再等待异步视觉模型。 |
| lxml **6.1.0** | 本地 XML 解析/树遍历，没有本项目需要的异步网络客户端 | 共用 XML 处理逻辑。线程用于保护事件循环响应，不等于所有解析都能绕开 GIL 获得线性吞吐。 |
| Pydantic **2.12.5** | 同步数据建模与校验 | 配置、供应商 DTO、请求/响应校验共享，不制造 Async DTO。 |
| Magika **1.0.2** | `magika/magika.py` 的 `identify_path/identify_bytes/identify_stream` 是同步方法；`_init_onnx_session` 明确使用 ONNX Runtime `CPUExecutionProvider`，路径/流识别还读取本地数据 | `engines/utils/file_detector.py` 共用一个同步探测入口，不建立无意义的 AsyncMagika 副本。URL/S3 下载完成后的识别、PDF Hook 的内容核验在异步调用边界使用 `run_sync`；同步 Loader 直接调用。 |
| Loguru **0.7.3** | `loguru/_logger.py` 的 `add` 支持 `enqueue=True` 与 coroutine sink；`_simple_sinks.py` 分别实现同步 StreamSink 和调度 coroutine 的 AsyncSink | `core/logging.py` 的日志 API 保持同步门面；文件 sink 已启用队列，stdout sink 仍直接 write/flush。无需为了同名日志操作再建 AsyncLogger；不能把当前配置描述为完全非阻塞，慢 stdout 会拖慢调用线程。异步 sink 如以后采用，还须在关停时等待 `complete()`。 |
| PyYAML **6.0.3** | `yaml/__init__.py` 的 `safe_load` 同步读取并构造数据 | `config/settings.py` 在 CLI、应用工厂或 worker 启动阶段读取配置；这是有界的配置加载边界，不是网络适配器，不需要复制 Async 配置类。若以后在运行中的请求路径热加载，应再由调用边界卸载文件读取。 |
| FastAPI **0.135.3** | `fastapi/routing.py::run_endpoint_function` 直接 await coroutine endpoint，只有同步 endpoint 才交给 `run_in_threadpool`；dependencies 同样区分执行方式 | 项目采用异步路由与 lifespan，框架本身不拆成两套应用类型。异步函数内部的同步 I/O **不会**被自动卸载；复核由此定位到 URL 入库准入的同步 DNS，修复与故障测试见下方记录。 |
| Uvicorn **0.42.0** | `uvicorn/server.py` 的 `Server.serve` 为原生 async；`Server.run` 在进程边界用 asyncio runner 驱动它，`uvicorn/main.py::run` 管理服务器启动 | `cli.py` 从同步命令行调用 `uvicorn.run` 是服务器拥有事件循环的边界，不属于需要移除的库内循环包装。ASGI 应用保留原生异步；无需新增 AsyncCLI 或 AsyncServer 门面。 |
| python-multipart **0.0.32** | `python_multipart/multipart.py` 的 MultipartParser.write 是同步增量解析；Starlette 的 `formparsers.py` 原生异步接收请求流，再逐块喂 parser 并等待文件操作 | 当前 `/ingest` 接收 JSON `IngestSubmit`，仓内 API 没有 File/Form/UploadFile/request.form 路径，因此该直接依赖暂未被应用使用。保留依赖，本轮不删除；后续可独立评估精简。无需为纯解析状态机造 Async 副本，也不能把 MinerU 的出站 multipart（HTTPX 编码）算作它的应用调用。 |
| Alembic **1.18.4** | `alembic/runtime/migration.py::MigrationContext.run_migrations` 是同步迁移执行；异步数据库连接可通过 SQLAlchemy 的 `AsyncConnection.run_sync` 运行迁移函数 | 项目 `alembic/env.py` 已使用 async engine、`await connection.run_sync(do_run_migrations)` 与 `await dispose()`，CLI 外层 `asyncio.run` 负责生命周期。这里的 `run_sync` 是 SQLAlchemy 的 greenlet 连接桥接，不是本项目线程工具；无需复制迁移脚本或建立 AsyncAlembic。 |
| Python 标准库 | `pathlib`、`zipfile`、本地文件、Base64 与 `socket.getaddrinfo` 等为同步工作；`asyncio.to_thread` 不能终止工作线程 | 文件读取/写入、媒体准入中的 DNS、较重解析在异步调用边界卸载。采用 `engines.offload.run_sync` 等待工作结束后传播取消，保护临时文件生命周期。 |

本矩阵只覆盖项目实际使用的方法，不能据此推断 SDK 的所有功能都已有异步实现。
后续新增适配器仍应先核查所需方法，而不是沿用“整个供应商支持/不支持 async”的标签。

## Milvus 连接时机变化

本地 PyMilvus 源码的 `AsyncMilvusClient.__init__` 保存连接配置；`_get_connection()`
在没有 handler 时等待 `_connect()`。官方异步上下文入口也会连接，但本项目直接持有
客户端并在操作时调用，因而连接发生在首次等待的 RPC。

因此，`await build_context(...)` 仍会先校验配置与资源组合，但不会因为创建
`AsyncMilvusClient` 就完成服务连通性检查。错误可能在第一次 `ensure_collection`、
检索或写入时暴露。`/admin/health` 仍是进程存活检查；验证部署需要实际读写验收。
此前同步管理客户端的构造副作用不应继续被当作健康保证。

后续第五批已以 PyMilvus 2.6.17 / 真实 Milvus 2.6.22 验证 47 项契约与生命周期
用例。确认服务端同参数建索引幂等后，索引异常不再自动删除集合；仅在必要字段、
类型、主键、auto_id、BM25 配置与维度兼容且索引确实缺失时，显式 ensure 才续建。
实机测试覆盖服务端提交后的响应丢失、部分索引取消后跨实例恢复，以及关闭一个客户
端后另一个继续读取；详细失败反例与最终质量门见 [第五批实施记录](v2_plan.md)。

只删除冗余客户端和桥接，不修改 collection schema、metadata、过滤语义、维度检查或
revision 一致性要求。原有网络超时与业务失败仍须由各自调用边界处理。

## 补充依赖复核发现

FastAPI 本地源码表明：`async def` 路由内部的同步调用仍在事件循环线程执行。
复核发现 `api/routes/ingest.py::submit_ingest` 曾直接调用 `SourcePolicy.check`，其 URL
分支最终执行同步 `socket.getaddrinfo`。慢 DNS 会阻塞同一进程中的其他协程；模型与
Loader 已使用线程准备边界，并不能自动保护这个更早的 HTTP 准入入口。

本轮在 API 边界使用现有 `run_sync(policy.check, payload.source)`，保持准入检查先于
任务创建，不改变安全策略，并以阻塞解析器故障注入验证事件循环响应和取消排干。
`SourcePolicy` 本身仍是共享的同步策略对象，避免为同一校验逻辑复制第二份实现。

Loguru 的 stdout sink 当前是同步写入，属于需保留在性能说明中的边界；本次未修改
日志拓扑。python-multipart 当前没有应用入站上传调用点，列为后续依赖精简候选，
本次不删除依赖，也不引入新的上传功能。

## 落地边界

| 范围 | V2 选择 |
|---|---|
| 模型 | `BaseEmbeddingModel` / `AsyncBaseEmbeddingModel`、`BaseReranker` / `AsyncBaseReranker`；4 组供应商模型各自分离，同名请求与关闭方法；每实例单一客户端。 |
| 来源 | `BaseLoader` / `AsyncBaseLoader`，Local / URL / S3 / AutoLoader 及 route 按模式分开；共享元数据、来源解析与资源对象。 |
| 文档提取 | Docx / MinerU 的提取器按模式分开；DocxConverter / DocxParser 等同步计算共用；清理器只有涉及视觉模型的入口按模式分开。 |
| Pipeline | `Pipeline` / `AsyncPipeline` 共享分块、结果构造等计算，`run/stream_run/batch_run/close` 同名；输入 Loader / 模型 / Hook 必须匹配模式。 |
| Hook | `HookRegistry` / `AsyncHookRegistry` 是类型，`PipelineHooks` / `AsyncPipelineHooks` 是默认实例。同步提取函数加入异步表必须显式调用 `sync_extractor`；纯分块 Hook 共享。 |
| 批量向量化 | 同步 `engines.embedding.batch.embed_documents`；异步 `engines.embedding.async_batch.embed_documents`。窗口、模型原生 batch_limit、并发预算仍分别控制。 |
| 向量存储 | 只有异步边界：`AsyncVectorStore`、`AsyncVectorSearchPort`、`AsyncKeywordSearchPort`；实现为 `AsyncInMemoryVectorStore` / `AsyncMilvusStore`。不额外提供事件循环包装的同步存储。 |
| 领域持久化与任务 | `DocumentStore`、`KnowledgeBaseRepository`、`RevisionStager` 等原生异步对象保留领域类型名，异步方法取消 `a` 前缀。TaskStore / TaskExecutor 等已有无前缀名称不改。 |
| 组合根 | `await build_context(...)` 和 `await context.close()`；不恢复 `abuild_context` 或同步包装。启动失败逆序回收、成功发布 Context 的规则保留。 |
| Gate | 必要的双模式基础原语：同步与异步实例绑定同一个对象、争用同一份预算。拆成两个独立 semaphore 会让总并发翻倍，因此不按适配器规则机械拆开。 |

## 故障与性能约束

1. **原生异步不等于无限并发。** 模型模板的 `@final` 入口仍统一取 gate，批量调度和
   来源加载仍有上限；拆成两个类型不复制进程预算。异步批量 embedding、Pipeline 和
   DOCX 视觉处理使用 `engines.concurrency.map_concurrently` 固定 worker 数量，失败或
   取消时排干已启动工作，避免只限制 semaphore 却预建任意多个 task。
2. **取消不能杀死线程。** `run_sync` 等待阻塞准备结束后再传播取消；慢磁盘、DNS、
   同步扩展函数会延长取消时间。这个等待换来“调用返回后可以释放输入资源”的明确边界。
3. **媒体准备和网络分开。** Qwen embedding/reranker 的本地读取、URL 准入与 DNS
   放在受保护的线程准备阶段；请求仍 await 原生客户端。准备期间取消后不再发 HTTP 请求。
4. **CPU 算法不复制。** 正规化、排序、分块与 XML 遍历只保留一份计算；Async 名称不
   表示 CPU 加速。需要吞吐扩容的 preprocessor 仍按进程扩容。
5. **协议形状不能证明执行模式。** 同名 Protocol 的运行时检查只看成员存在性，不能
   代替 coroutine 检查。共享 DocxConfig 的视觉模型在清理器、提取器和组合根用
   `validate_vision_model` 验证 `describe` 的执行形态；两方向错配都在联网前 TypeError。
6. **注入不转移底层客户端所有权。** 模型/Loader 的 `client=` 由调用者关闭，未注入才
   自行创建并持有。Context 对适配器的托管与适配器对其底层 client 的所有权是两层契约。

## 验证与迁移入口

- 模型故障测试覆盖：错误模式客户端构造、借用客户端关闭、构造/关闭异常、原生异步网络、
  同步/异步实例共享 gate、网络取消释放 gate，以及本地文件/URL 准入线程取消排干。
- 文档守卫读取 README 与递归 docs Markdown：检查语法、项目符号、真实签名、已解析对象的
  方法存在性、直接异步调用等待方式，以及能够识别的客户端/模型/Loader/Hook 模式匹配。
  它不执行远程请求，也不是完整类型推导；复杂别名与跨代码块变量仍依赖审阅或类型检查。
- [版本说明](../docs/release_notes.md) 列出破坏性迁移表；
  [模型用法](../docs/model_usage.md) 与 [Pipeline 用法](../docs/pipeline_usage.md)
  使用新 API。仅旧名称的历史说明保留，旧可调用别名不恢复。

本次专项验证：模型 94 项通过；文档守卫 154 项通过、15 项因代码块没有可检查的
项目调用而跳过。临时副本反向验证确认：漏 await、调用已移除方法、同步模型注入
异步 client 各使文档守卫失败；把受保护的媒体准备退回裸 `to_thread` 后，embedding /
reranker 的本地文件与 URL 准入四个取消用例全部失败。全仓最终质量门另见 V2 实施记录。


## 第十四批补漏：任务入库的同步计算与清理

复核发现第四批已让 AsyncPipeline 使用 run_sync，但 IngestRunner 的正规化、分块仍
直接 await asyncio.to_thread，LoadedResource.cleanup 仍直接在事件循环执行。取消协程
不会终止线程，因而任务阶段可能先退出，甚至在正规化仍使用输入时删除临时文件；同步
清理缓慢时也会阻塞其他任务。

第十四批用已有 run_sync 替换这三处调用，不新增执行模型或清理抽象。同步计算仍只保留
一份实现；取消后先等线程结束，消费迟到异常，再传播取消，不把迟到产物写入 context。
通过阻塞线程、重复取消、晚到异常及真实 Context 关停验证；详细结果见
[v2_plan.md 第十四批](v2_plan.md#第十四批入库线程取消与资源清理)。

该修复不改变生产 revision 激活/GC，也不代表其它向量数据库已经接入。
