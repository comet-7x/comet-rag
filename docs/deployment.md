# 部署

## 先选形态

| | 单进程 | 跨进程 |
|---|---|---|
| 依赖 | 无（连 Redis 都不要） | PostgreSQL + Redis + Milvus |
| 进程 | 1 个 | API + preprocessor + embedder |
| 任务重启后 | 丢失 | 从断点续跑 |
| 适合 | 开发、演示、小规模 | 生产 |

两种形态**跑的是同一份代码**，差别只在 `backends` 配置段。

`task_executor: arq` 必须同时配置 `task_store: postgres` 和
`vector_store: milvus`。内存后端无法跨 API 与 worker 共享数据，配置解析时会拒绝
这种组合；单进程仍可使用内存后端。

---

## 单进程

```bash
uv sync --extra server
uv run comet-rag serve
```

```yaml
# config.yaml
server_config: { app_name: comet-rag, host: 0.0.0.0, port: 8000 }
infrastructure_config:
  embedding_model:
    base_url: "http://127.0.0.1:8080/v1"
    model_name: qwen3-vl-embedding
    dim: 4096          # ⚠️ 必须与模型实际输出一致，见下方「维度」
backends:
  vector_store: memory
  task_store: memory
  task_executor: inprocess
```

任务跑在 API 进程自己的协程里，重启即丢 —— 这正是它只适合开发的原因。

---

## 跨进程

### 1. 起中间件

```bash
docker compose up -d          # postgres + redis + milvus(+etcd+minio)
uv run alembic upgrade head   # 建表
```

### 2. 配置

```yaml
server_config: { app_name: comet-rag, host: 0.0.0.0, port: 8000 }
infrastructure_config:
  embedding_model:
    base_url: "http://127.0.0.1:8080/v1"
    model_name: qwen3-vl-embedding
    dim: 4096
  reranker:            # 可选：没配就跳过重排，检索仍可用
    base_url: "http://127.0.0.1:8081/v1"
    model_name: qwen3-vl-reranker
  database: { host: localhost, port: 5432, username: comet, password: ***, database: comet_rag }
  redis:    { host: localhost, port: 6379, db_index: 0 }
  vector_database: { endpoint: "http://localhost:19530", database_name: comet_rag, collection_prefix: comet, replica_number: 1 }
backends:
  vector_store: milvus
  task_store: postgres
  task_executor: arq
limits:
  model_concurrency: 8      # 本进程对模型服务的**总**并发
  model_queue: 256          # 闸门外的等待席位
  model_image_max_bytes: 20971520  # 本地模型图片上限（20 MiB）
  max_backlog: 1000         # 待执行任务上限，超了返回 429
  task_chunk_context_max_bytes: 33554432  # CPU/IO worker 交接 payload 上限
```

### 3. 起进程

```bash
uv run comet-rag serve                  # 生产端：只投递，不执行
uv run comet-rag worker preprocessor    # 消费端：解析 / 清洗 / 分块
uv run comet-rag worker embedder        # 消费端：向量化 + 写库
```

**两个 worker 都必须起。** 一条流水线会在 `chunking` 与 `indexing` 之间从
cpu 道移交到 io 道；只起一个的话，任务会静静停在另一条队列上 ——
状态 PENDING、无错误、无日志，从 API 上完全看不出来。
排查"任务不动了"时，第一件事就是确认两个 worker 都在跑。

### 4. 扩容

```bash
# preprocessor：加进程。副本数 ≈ 可用 CPU 核数
uv run comet-rag worker preprocessor &   # ×N

# embedder：加并发，**不要**加进程
#   调 limits.model_concurrency 与 workers/embedder.py 的 max_jobs
```

**反过来会真的更慢**，而且症状具有欺骗性：给 embedder 加进程会把对模型服务
的连接数乘以进程数，请求在服务端排队，每个进程只看到"上游变慢了"，
看不出是自己造成的；服务端的动态批处理也被打散。

---

## 启动入口：一个真实的坑

```bash
uv run comet-rag serve                    # ✅ 用 config.yaml 里的 host/port
uv run uvicorn comet_rag.api.main:app     # ⚠️ 用 uvicorn 默认的 127.0.0.1:8000
```

**两条命令、同一份配置、两个结果。** 根因不是 bug，是职责划分：host/port 属于
**服务器**、不属于 ASGI 应用；uvicorn 命令行直接拿走 app，配置里那两个值根本
没机会参与。

所以 `comet-rag serve` 是唯一推荐入口。`uvicorn` 那条留给需要 `--reload` 的
开发场景 —— 而 `comet-rag serve --reload` 也支持，且会把配置路径经环境变量
传给子进程。

### 配置文件路径

优先级：`--config PATH` > `$COMET_RAG_CONFIG` > `./config.yaml`

```bash
comet-rag serve --config /etc/comet-rag/prod.yaml
COMET_RAG_CONFIG=/etc/comet-rag/prod.yaml comet-rag worker embedder
comet-rag config --config /etc/comet-rag/prod.yaml   # 打印生效配置（密码已脱敏）
```

排查配置问题时先跑 `comet-rag config`：它打印的是**真正生效的那一份**，
省掉"我改的是不是这个文件"这一轮。

---

## 暂停入库准入

维护/迁移时设置 `ingest_policy.accept_new_tasks: false` 并部署到所有 API 实例。
新入库与两种 ingest kind 的手动重试返回 503，已受理任务继续执行；状态可查询
`/admin/limits.accept_new_ingest_tasks`。它不阻止旧 API、库调用、自定义生产者或在途请求，
也不取消自动重试，不是远端写入排干证明。恢复设为 true 后重新部署。
新命名空间、历史缺载荷处置与生产切换范围见 [迁移说明](revision_migration.md)。

## 入库来源准入（安全）

`POST /ingest` 的 `source` 是调用方给的字符串，会被交给 loader 去读文件或发请求。
**默认是拒绝的**：

| 来源 | 默认 | 说明 |
|---|---|---|
| 服务器本地路径 | ❌ 拒绝 | 开了等于给调用方一个任意文件读取通道 |
| http/https 公网 | ✅ 允许 | |
| 私网 / 环回 / 链路本地 | ❌ 拒绝 | 挡 SSRF；`169.254.169.254` 上有云凭据 |
| `s3://` / `minio://` | ❌ 拒绝 | 显式开启，并建议限定 bucket |
| 其他协议（`file://` 等） | ❌ 拒绝 | 绕过本地检查的常见写法 |

单机部署想用"把服务器上的文件入库"这个功能，显式打开并**圈定范围**：

```yaml
ingest_policy:
  allow_local: true
  local_roots: ["/data/corpus"]     # 留空 = 整个文件系统，生产务必配置
  allowed_url_hosts: []             # 需要时再收紧到具体主机
  allow_private_network: false      # 内网抓取才打开，会关掉 SSRF 防护
```

`local_roots` 的包含性检查在**展开符号链接与 `..` 之后**做，所以
`/data/corpus/../../etc/passwd` 与指向外部的软链都会被挡下。
重定向也逐跳校验 —— 只查入口 URL 挡不住"公网地址 302 到内网"。

要从 MinIO 或 AWS S3 入库，连接配置与准入策略必须同时存在：

```yaml
infrastructure_config:
  s3:
    endpoint_url: "http://localhost:9010"  # AWS S3 可留空
    access_key_id: minioadmin              # 也可留空，使用 SDK 默认凭据链
    secret_access_key: minioadmin
    region_name: us-east-1
    addressing_style: path                 # MinIO 通常使用 path
    max_object_bytes: 104857600             # 100 MiB

ingest_policy:
  allow_s3: true
  allowed_s3_buckets: ["documents"]
```

调用时把来源写为 `s3://documents/report.docx` 或
`minio://documents/report.docx`。Loader 会先用 `HEAD` 拒绝已知超限对象，
下载时再按实际累计字节检查，避免伪造或过期的 `ContentLength` 绕过限制。
服务使用 `AsyncS3Loader` 和 aioboto3 原生异步客户端；临时文件与该客户端由应用上下文统一清理。同步库调用使用单独的 `S3Loader`，不会创建异步客户端。

被拒返回 **403**，且错误信息里不回显解析出来的 IP：那等于把内网探测结果
送给调用方，防护会退化成一个好用的扫描器。

---

## PDF 与外部 MinerU

PDF 提取不是 Comet-RAG 进程内能力。先独立部署 `mineru-api` 或
`mineru-router`，确认 `GET /health` 返回 `protocol_version=2`，再启用配置：

```yaml
infrastructure_config:
  mineru:
    enabled: true
    base_url: "https://mineru-router.example.internal"  # 或单实例 mineru-api
    backend: vlm-http-client               # 本地 pipeline 部署可改为 pipeline
    parse_method: auto
    language: ch
    formula: true
    table: true
    connect_timeout_seconds: 10.0
    request_timeout_seconds: 60.0
    upload_timeout_seconds: 300.0
    poll_interval_seconds: 1.0
    parse_timeout_seconds: 900.0
    max_response_bytes: 16777216
    max_markdown_bytes: 8388608
limits:
  mineru_concurrency: 2
  mineru_queue: 16
  mineru_wait_timeout: 30.0
```

`base_url` 必须是 MinerU 文档服务，不能是只提供 `/v1/models` 的 vLLM。使用远端
MinerU VLM 时，在 `mineru-api` 进程侧设置 `MINERU_VL_SERVER` 和
`MINERU_VL_MODEL_NAME`，不要允许 `/ingest` 调用方传模型地址。

跨进程部署中，PDF 的外部等待目前仍属于 preprocessor 的 `extracting` 阶段，
会占用一个 worker job 名额，但不会阻塞 asyncio 事件循环。M2 实测两个单页样本的
CPU lane 持有比例都超过 99%，因此不要仅靠提高 preprocessor 并发来掩盖慢上游；
先观察 `/admin/limits` 的 `mineru_gate`，再扩 MinerU/Router 容量。按格式动态分道
会改变任务路由语义，留给有长文档生产数据后的独立规格。

完整的单实例、远端 vLLM、Router、协议验证和集成测试命令见
[MinerU 集成](mineru_integration.md)。

---

## dense、keyword 与 hybrid 检索

`POST /search` 默认 `mode=dense`，所以旧调用无需修改。精确术语可选 `keyword`，
希望合并语义与关键词候选时选 `hybrid`：

```bash
curl -X POST localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{
    "kb_id": "demo",
    "query": "TraitObject 内存布局",
    "mode": "hybrid",
    "top_k": 5,
    "fetch_k": 20,
    "filter": {"language": ["zh", "en"]},
    "rerank": true
  }'
```

`fetch_k` 是每路进入融合/重排前的候选上限，范围 1～500，且不会小于 `top_k`；
缺省为 `max(top_k × 4, 20)`。hybrid 并发召回 dense 与 BM25，用 RRF 按排名融合，
不直接相加不可比的原始分数。

响应重点字段：

```json
{
  "mode": "hybrid",
  "channels": ["dense", "keyword"],
  "reranked": true,
  "fetched": 20,
  "degraded": null,
  "degradations": [],
  "chunks": [{
    "id": "...",
    "vector_score": 0.82,
    "keyword_score": 6.31,
    "fusion_score": 0.0325,
    "vector_rank": 2,
    "keyword_rank": 1
  }]
}
```

`mode` 与 `channels` 描述实际执行结果：hybrid 某一路暂时不可用时，服务返回另一路，
并在 `degradations` 标明失败阶段和异常类型；两路都失败才返回 503。`degraded` 是
系统负载降级级别，与请求级通道故障不是一回事。原始异常消息只写日志，不回给客户端。

### 查询共享预算

`retrieval.timeout_seconds` 默认 30 秒，从 RetrievalService.search 入口计时，覆盖 KB 检查、
模型排队、召回/判活、重排和父块读取。阶段只使用剩余预算；重排和父块超时退回已有
候选，hybrid 单路超时可保留另一条成功通道，没有安全召回结果则返回 HTTP 504。
取消后等待清理，线程无法强杀，因此实际返回可能超过预算。
完整边界、429/503/504 区分和诊断见[查询 deadline](query_deadline.md)。

### Revision 读侧（实验性，生产尚未启用）

查询新增 `return_level=child|parent`，默认 child。parent 模式先对子块融合/重排，
再对完整候选按父块去重、批量回填正文，最后截取 top-k。返回的 `matched_child_id`
指向命中子块，所有相似度/融合/重排分数仍属于 child；普通 child 结果该字段为 null。
旧平坦记录没有 parent 时仍返回 child，父块缺失/错配或读取故障也退回 child，并在
`degradations` 记录 `stage=parent`，不伪造正文。

`config.yaml` 的 revision 读开关默认关闭；查询工作预算默认 30 秒：

```yaml
retrieval:
  timeout_seconds: 30.0
  revision_aware: false
  max_revision_candidates: 2048
```

在隔离测试环境显式开启后，CLI/API/worker 都走同一组合根：Memory 向量配 Memory
DocumentStore，Milvus 配 PostgreSQL DocumentStore 和 Strong。Milvus 模式要求
`backends.task_store=postgres`、SQL/Milvus 连接配置，并先应用三表迁移。
候选预算必须是 1～2048 的整数，读开关须为布尔值；拼错字段或未实现的写开关会报错。
此配置**单独不启用 revision 写入或激活**。第二十一批的新命名空间写入需要下文配套配置，
现有生产配置仍不默认开启。

仍可向 `await build_context(..., document_store=...)` 显式注入资源；这也是显式启用读侧。
启动前拒绝持久化资源错配，以及自行注入的非 Strong AsyncMilvusStore。组合根拥有自动创建
的 SQL 连接池，停止执行器/关闭适配器后才释放它；外部注入的 Database 由注入方管理。

每通道最多两轮、2048 个候选，先批量读取 active 指针，再参与 RRF/重排；staging、
retired 和被新版本替代的 legacy 均不可见。旧候选挤满上限时允许少返回，诊断为
`revision_candidate_cap`。判活存储故障或结果不完整时返回安全 503，**不会降级为返回
全部版本**。未配置 DocumentStore 的旧链路遇到 revision candidate 也拒绝查询。
独立激活/GC 和 opt-in 新命名空间生产接线已验收，但实际 legacy 迁移、目标部署切换
及完整 H/I 尚未完成，默认配置仍不启用新写入。GC 默认每轮最多 32 个 retired/abandoned。
独立 Task 阶段已持久化发布/清理意图，superseded 登记前失败从
cleanup_pending 重试，不能仅依赖候选扫描。purged 后的迟到写入需要保留身份并显式
重放精确清理；complete 只表示上次核验为空。生产开启前还需落实意图保留、分页扫描
及旧写排干/持续对账策略，不能提前删除 Task 后再依赖其 context 做恢复。
第十八批已接入 TaskService 删除保留守卫，并提供独立、带持久化游标的有界对账页。
源任务与扫描游标当前无自动到期日，force 不绕过保留；没有新加 HTTP 删除端点。
第十九批先持久化准备意图再写父块；新记录即使缺完整 handoff，取消后也可精确回收。
旧协议既无准备意图、也无 handoff 的记录仍保留并告警。完整策略见
[持续对账设计](../tasks/v2_reconciliation_design.md)。

### 独立 revision 对账

默认关闭，不改变生产 ingest 或 revision 读开关。自动模式要求完整的
ARQ/PostgreSQL/Milvus 配置，由组合根创建配套 Task/Document/VectorStore；不接受
不透明存储注入。同一来源 kind 的游标绑定 endpoint/database/prefix 摘要，配置切换
命名空间会拒绝启动，不能悄悄把同一批来源交给另一组存储。

```yaml
revision_reconciliation:
  enabled: false
  task_kind: null
  page_size: 8
  timeout_seconds: 30.0
```

开启前，task_kind 必须是实际采用 RevisionTaskStages 的来源 kind；新生产路径为
`revision-ingest`，默认 `ingest` 仍是旧路径。单独配置维护不会启用新 writer。
API/worker 必须共享相同配置与存储，且至少运行一个 IO worker。
启用前先升级所有参与 PENDING 补投的 worker；旧版本不认识维护 kind 的排除规则。
回退到旧版本前也需先停用并排干维护，保留游标并明确其迁移处置，不能假设跨版本混跑安全。

开启后仅 embedder 挂载原生 ARQ cron，每分钟第 23 秒及启动时处理一页；单进程
serve 不加载租约维护，也不启动后台循环。页上限 32 项/30 秒，cron 外层 45 秒，
失联由原 90 秒租约回收恢复。维护游标不会作为业务任务补投到队列。
业务 backlog 按本 Context 已注册的业务 kind 统计，保留的维护游标不占业务准入额度。

普通暂停使用配置并排干旧 IO worker；重启/重新启用继续原游标。显式取消维护 Task
表示终止该维护身份，不能用普通业务 retry 复活；需要经核验的维护迁移。

`GET /admin/limits` 的 revision_reconciliation 提供 task_id/status、last_page、error_code
和 overdue。超过 180 秒未确认一页即 overdue；认领、重试、心跳不算完成进度。
监控规则应覆盖整页失败、last_page.failed / last_page.missing_payload 非空、overdue，
以及配置开启却没有 IO worker。missing_payload 是需要恢复证据的历史项，不是可正常延期。
日志事件 `revision_reconciliation_failed` / `revision_reconciliation_partial_failure` /
`revision_reconciliation_missing_prepare` 分别对应整页失败、坏记录和历史恢复缺口。
`revision_reconciliation_recovery_required` 为已确认页中的缺载荷发出错误信号。最近页会被
覆盖，旧 result 可能缺少新字段；日志/处置清单需持久采集，不把后一页为空当作已解决。
项目提供告警信号，不部署通知中间件；无人执行时需通过外部轮询发现无进展。

### 新命名空间生产入库

第二十一批提供默认关闭的 revision_ingestion。启用后 POST /ingest 选择独立的
revision-ingest kind，注册五阶段 runner；必须配套 ARQ/PostgreSQL/Milvus、判活读侧、
相同来源的维护及新 KB 清单。同一 TaskStore 中发现 legacy ingest 任务会拒绝启动。
完整配置、绑定/预算、父子块与失败语义见
[生产 revision 配置](revision_migration.md#生产-revision-runner-配置)。
`/admin/limits.ingest_kind` 可确认当前进程选择，不能据此证明全部署一致或资料已迁移。

### Milvus schema 兼容

Milvus 必须使用 2.6 版本线的新 BM25 schema。旧 collection 缺少 chinese analyzer、
BM25 function 或 sparse index 时会返回 `CollectionSchemaMismatch`；服务不会自动删除或
改写已有数据。读路径会返回 HTTP 409，写路径的 indexing 任务会永久失败。升级时还需
把已移除的 `collection_name` 配置换成必填的 `database_name` 与 `collection_prefix`。
确认数据可重建后，由操作者显式删除旧 collection 并重新入库；完整步骤见
[M3 版本说明](release_notes.md#milvus-schema-v2-是破坏性变更)。

---

## 运维

### 看限流与降级

```bash
curl localhost:8000/admin/limits
```

```json
{
  "model_gate": {"limit": 8, "in_flight": 8, "waiting": 42, "rejected": 3, ...},
  "mineru_gate": {"limit": 2, "in_flight": 2, "waiting": 4, "rejected": 0, ...},
  "backlog": {"pending": 137, "max_backlog": 1000},
  "degradation": {"level": "NO_RERANK", "failure_rate": 0.31, ...}
}
```

- `in_flight` 贴着 `limit` → 下游是瓶颈
- `waiting` 一直很高 → 该加 worker 或提高上限了
- `rejected` 在涨 → 已经在拒请求
- `mineru_gate` 为 `null` → PDF 外部提取未启用
- `level` ≠ NORMAL → 服务正在降级运行，检索质量已经打了折

`/admin/health` **刻意不查下游**：探针查下游会让一次数据库抖动把整个服务从
负载均衡里摘掉，故障面反而变大。

### 降级顺序

资源紧张时按序自动降级，每次级别变化都打 WARNING 日志：

```
NO_RERANK → LOWER_TOP_K → REJECT_WRITES
关掉重排      砍 top_k       拒绝新入库任务
```

先砍最贵、最可有可无的。拒绝写入放最后 —— 那是唯一让用户"什么也得不到"的一级。
降级后指标回落也不会立刻恢复（要过冷却期），否则会在阈值边界上反复抖。

### 崩溃恢复

任务执行期间由执行器每 10 秒持续续租，长时间等待 MinerU 或模型响应也会续租。
续租在 CAS 中复验 worker 与尝试次数；失去所有权或续租失败时停止当前 runner。
runner 的 checkpoint 继续负责协作取消和阶段边界检查。

runner 写 context、进度和阶段留痕，以及执行器写终态、重试和移交时，都在
每轮 CAS 中校验 worker 与尝试次数。取消已受理时优先落 CANCELLED，不再
把任务改成成功或重新排队；关停也不会取消已被别的 worker 接管的任务。
同实例移交后再次认领另有本地令牌，防止退还 attempts 后旧收尾误认新尝试。
这些围栏只保护任务记录，不能撤回已经发出的模型请求或向量写入。

worker 被 `kill -9` 后，它手上的任务会停在 RUNNING。租约回收（默认 90s）
会把它退回队列，由另一个 worker 接管续跑，**已完成的阶段不会重做**。

定时器挂在每个 worker 上，arq 的 cron 是 unique 的，多副本下每个时刻只有
一个真正执行，不必为它单开进程。

回收使用的候选是旧快照，因此实际更新时还会复查心跳、状态和所属尝试，
已续租的候选跳过。租约仍不是准确的死亡判定，外部写入需要自己的版本围栏。

同一个定时器也对账 PENDING：每轮最多扫描 100 条，执行器实例轮转分页。
因此 API 建任务后退出、Redis 初次投递失败、重试/移交投递失败，都会在后续
成功扫到这条记录时补投。补投只恢复队列消息，不增加 attempts；恢复时限取决于
积压、worker 是否有执行槽及中间件可用性，不保证所有任务一分钟内恢复。

队列载荷仍只有 task_id。job ID 携带 PENDING 版本，过期消息不能认领新的
重试；人工重置次数也不会撞上旧 ARQ 结果键。消费端在认领前失败留下的传输
结果键可被补投移除，任务结果始终以 TaskStore 为准。

业务重试从持久化的错误发生时间恢复指数退避，各进程必须使用同一
retry_backoff。过期退避立即执行，租约回收和人工 retry 不附加业务退避。
调用适配器 `submit(delay=...)` 的额外自定义延迟只存在 Redis，不属于本批
持久化恢复保证；业务如需预约任务，应先设计持久化调度时间，不能依赖它。

升级时应一起更新生产端与 worker：旧版无版本 job ID 会被新 worker 跳过，
由 PENDING 对账补投。初次投递失败仍向调用方报错，但数据库记录可能已创建；
客户端重试应复用 idempotency_key，避免重复创建入库任务。

### Milvus 连接时机

`AsyncMilvusStore` 仅使用 PyMilvus 的 `AsyncMilvusClient`，集合检查、索引描述和加载
也使用原生异步 RPC。构造器校验配置，SDK 在首次被等待的 RPC 时建立连接，因此
`await build_context(...)` 成功不代表 Milvus 已可达。首次创建知识库、入库或检索仍
可能遇到连接错误；`/admin/health` 保持进程存活探针语义，不替代真实读写验收。

建索引或加载失败时，适配器保留集合并传播原始错误。网络超时不能证明服务端未完成，
集合此时也可能已被其他实例写入，因此不能靠自动删除回滚。再次调用
`ensure_collection` 时，只有维度一致、字段/function/analyzer 兼容且 sparse 索引
确实不存在，才用相同参数补建索引；已有错误索引、未知索引详情和旧 schema 仍拒绝
修改。读写方法仅校验，不触发自动续建。真实验证命令见 [测试说明](../tests/README.md)。

### 优雅关停

`SIGTERM` 后：停止收新任务 → 协作取消在跑的 → 等它们落到一致状态 → 释放连接。
关停等待超时会请求取消协程，但仍须排干已启动的同步线程，再关闭依赖；这不是强制
终止线程的硬时限。进程被强制终止后，跨进程部署由 worker 租约回收恢复任务；
单进程部署不注册租约回收。

IngestRunner 的正规化、分块和临时文件清理复用 run_sync。取消或 Context.close
必须等待同步工作结束；正规化完成前不删除输入，分块取消后不发布迟到的 drafts，
清理期间重复取消也不提前退出。文件清理在工作线程执行，保持事件循环可响应。

V2 的组合根入口为 `await build_context(...)`，只提供异步构造；早期过渡名称 `abuild_context` 已移除。
自定义启动代码需更新导入并在原事件循环中等待；CLI、API lifespan 与 worker 已迁移。
所有配置校验先于资源创建。初始化失败会等待本次自建资源回收，注入对象仍由调用方管理；
成功返回后由 Context 统一关闭，调用方应在 `finally` 中 `await context.close()`。

关闭时先排干执行器，再撤销注册和关闭依赖；重复关闭不会重复释放资源。
取消启动回滚或关闭的调用方，也要等清理完成后才收到取消异常。这个保证不覆盖进程被
强制终止，也不把有错误的第三方 `close()` 变成必定释放成功：清理错误会被记录。

---

## 维度（最容易出事的一项）

`infrastructure_config.embedding_model.dim` **必须与模型实际输出一致**。

- 建 collection 时用它；
- 写入时校验（spec A12）；
- 建库后**不能改** —— 改了就得重灌整个知识库。

同维度的不同模型谁也拦不住：混用不报错，只是检索质量静默劣化，事后还分不清
哪些 chunk 该重算。所以知识库元数据里记了建库时用的模型名，对不上会直接拒绝入库。

确认维度：

```bash
curl -s http://127.0.0.1:8080/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-vl-embedding","input":"test"}' \
  | python3 -c "import json,sys; print(len(json.load(sys.stdin)['data'][0]['embedding']))"
```
