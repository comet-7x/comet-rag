# 部署

## 先选形态

| | 单进程 | 跨进程 |
|---|---|---|
| 依赖 | 无（连 Redis 都不要） | PostgreSQL + Redis + Milvus |
| 进程 | 1 个 | API + preprocessor + embedder |
| 任务重启后 | 丢失 | 从断点续跑 |
| 适合 | 开发、演示、小规模 | 生产 |

两种形态**跑的是同一份代码**，差别只在 `backends` 配置段。

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
  vector_database: { endpoint: "http://localhost:19530", collection_name: unused }
backends:
  vector_store: milvus
  task_store: postgres
  task_executor: arq
limits:
  model_concurrency: 8      # 本进程对模型服务的**总**并发
  model_queue: 256          # 闸门外的等待席位
  max_backlog: 1000         # 待执行任务上限，超了返回 429
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
临时文件与同步/异步 S3 client 由应用上下文统一清理。

被拒返回 **403**，且错误信息里不回显解析出来的 IP：那等于把内网探测结果
送给调用方，防护会退化成一个好用的扫描器。

---

## 运维

### 看限流与降级

```bash
curl localhost:8000/admin/limits
```

```json
{
  "model_gate": {"limit": 8, "in_flight": 8, "waiting": 42, "rejected": 3, ...},
  "backlog": {"pending": 137, "max_backlog": 1000},
  "degradation": {"level": "NO_RERANK", "failure_rate": 0.31, ...}
}
```

- `in_flight` 贴着 `limit` → 下游是瓶颈
- `waiting` 一直很高 → 该加 worker 或提高上限了
- `rejected` 在涨 → 已经在拒请求
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

worker 被 `kill -9` 后，它手上的任务会停在 RUNNING。租约回收（默认 90s）
会把它退回队列，由另一个 worker 接管续跑，**已完成的阶段不会重做**。

定时器挂在每个 worker 上，arq 的 cron 是 unique 的，多副本下每个时刻只有
一个真正执行，不必为它单开进程。

### 优雅关停

`SIGTERM` 后：停止收新任务 → 协作取消在跑的 → 等它们落到一致状态 → 释放连接。
超时未收尾的交给租约回收，不会留下 RUNNING 僵尸。

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
