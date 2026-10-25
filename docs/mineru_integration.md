# MinerU 集成

Comet-RAG 的 PDF 链路已经在 M2 完成。它只通过 HTTP 连接外部
`mineru-api` / `mineru-router`，不会安装、导入或启动 MinerU SDK、Torch、模型
权重和 GPU 运行时。

```text
Local / URL / S3 PDF
        │
        ▼
Comet-RAG Loader → DocumentExtractorPort → Normalizer → 分块 → 向量化 → 入库
                         │
                         │ /health · /tasks · /tasks/{id} · /result
                         ▼
                 mineru-api / mineru-router
                         │
                         ▼
                 本地模型或远端 vLLM
```

边界很明确：Comet-RAG 管来源准入、任务重试、并发限制、入库和检索；MinerU
服务管 PDF 解析、模型生命周期、GPU 调度和服务端产物清理。`/ingest` 调用方不能
覆盖 MinerU 地址、backend 或解析选项，避免把解析服务变成 SSRF 代理。

## 协议要求

适配器要求 MinerU API protocol v2：

1. `GET /health` 必须返回 `status=healthy`、`protocol_version=2` 和非空版本号；
2. `POST /tasks` 流式上传一个 PDF，并以 202 返回 `task_id`；
3. `GET /tasks/{task_id}` 轮询 `pending`、`processing`、`completed` 或 `failed`；
4. `GET /tasks/{task_id}/result` 返回唯一一项 `results.*.md_content`，并可同时返回 legacy
   `content_list`。

`md_content` 始终是正文真相。M4 起适配器显式请求 content list，仅用它验证并生成真实
页边界：可支持项按页重建后必须与 Markdown 完全一致，否则不产生页事实并回退标题
section。这是整份文档的保守判定；即使正文均为文本，封面的图片或勘误表也可能使
整份文档没有 page blocks。不能从页内文本搜索、页码样式或 PDF 总页数猜测区间。
适配器仍关闭 ZIP、图片、中间 JSON、模型输出与原文件回传，不依赖 MinerU 默认值。`POST /file_parse` 是同步兼容接口，不是 Comet-RAG 的生产路径。

先确认你拿到的是 MinerU 文档服务，而不是底层 vLLM：

```bash
curl http://127.0.0.1:8989/health
```

健康响应中必须包含 `protocol_version: 2`。仅有 `/v1/models` 和
`/v1/chat/completions` 的地址是 OpenAI 兼容模型后端，不能直接填入
`infrastructure_config.mineru.base_url`。HTTP 200 本身不证明协议正确：vLLM 的
`/health` 可以返回空正文；文档服务必须返回上面的 JSON 字段。既有地址若带 `/v1`，
也不要自动删除前缀重试上传——可能是反向代理路径，先核对服务角色与路由。

## 可选的复杂 PDF 逐页部署扩展

上游3.4.5的HTTP默认输出没有逐页Markdown。第二十九批提供显式关闭跨页合并、复用
上游渲染器的[独立部署扩展](../deploy/mineru/README.md)，仅验收单实例文档API的
`vlm-http-client`完整PDF。它不是上游已发布功能，也不要求应用安装MinerU SDK。
health中的`extensions.comet_page_markdown=1`与结果中的同名版本化字段共同生效；
应用严格校验物理页、空页、完整性及全文重建，错误扩展不静默回退。此模式关闭冗余
content list回传，仍遵守现有总超时和响应预算。未声明扩展的服务保持上述旧行为。

完整7/80页公开文件的原生页事实已补验，但不等于人工质量、固定权重或目标部署验收；
证据和限制见[第二十九批报告](evaluations/mineru_pages_20261025.md)。扩展可能改变跨页
合并及canonical正文，按新命名空间重建和切换，不能在线改开关后复用旧索引。

## 部署 mineru-api

本协议已实测的上游版本是 **MinerU 3.4.5 / protocol v2**。在 Comet-RAG 之外的
专用环境固定 `mineru==3.4.5`；不要在应用虚拟环境安装，也不要用不带版本的安装命令
自动跟进上游主分支。上游 4.x 已调整目录和入口，需要另行核对协议，不能从同名 CLI
推断兼容。见[官方旧版本兼容说明](https://opendatalab.github.io/MinerU/usage/compatibility/)。

```bash
uv venv /path/to/mineru-api-env --python 3.12
uv pip install --python /path/to/mineru-api-env/bin/python 'mineru==3.4.5'
```

以下命令在该独立环境运行；`vlm-http-client` 使用远端模型时，本轮基础安装不含
Torch/vLLM/transformers。`pipeline` 或进程内 GPU 模型需要上游对应 extras，不能
把只装基础包的 HTTP 模式验收当成这些 backend 已可用。

单实例或开发环境直接运行 `mineru-api`。如果模型由该进程自行管理：

```bash
mineru-api --host 127.0.0.1 --port 8989
```

如果已有 OpenAI 兼容的 MinerU 2.5 vLLM，则让外部网关连接它：

```bash
MINERU_VL_SERVER=http://<vlm-host>:<vlm-port> \
MINERU_VL_MODEL_NAME=opendatalab/MinerU2.5-2509-1.2B \
mineru-api --host 127.0.0.1 --port 8989 --max-concurrency 2
```

对应的 Comet-RAG backend 是 `vlm-http-client`。当前 MinerU 用上述环境变量配置
模型地址；不要把 `server_url` 开放成每个入库请求都能修改的参数。上例将单个远端
VLM 客户端并发限为 2；API 任务并发和处理窗口是不同预算，可在外部服务分别设置
`MINERU_API_MAX_CONCURRENT_REQUESTS=1`、`MINERU_PROCESSING_WINDOW_SIZE=2`。
它们不替代 Comet-RAG 的进程级闸门。

MinerU 在绑定 `0.0.0.0` 或 `::` 时默认禁用 `*-http-client` 和请求级
`server_url`。确需远程访问时，`--allow-public-http-client` 是风险确认开关，
**不是鉴权**；服务仍应放在私网并由 ACL 或认证代理保护。

## 部署 mineru-router

多实例或多 GPU 使用 `mineru-router`，Comet-RAG 只连接 Router 的统一地址。
Router 会维护 `task_id → worker` 的亲和关系；不能用普通轮询负载均衡器替代，
否则后续状态查询可能落到另一进程并返回 404。

让 Router 管理本机 GPU：

```bash
mineru-router --host 0.0.0.0 --port 8002 --local-gpus auto
```

绑定 `0.0.0.0` 只表示监听所有网卡，不代表接口已经受保护。Router 必须部署在
私网，并至少由网络 ACL、防火墙或带身份认证的反向代理限制访问；对 Comet-RAG
提供远程地址时还必须由代理终止 TLS。`--allow-public-http-client` 只解除 MinerU
的 SSRF 防护限制，**不是身份认证或访问控制机制**。

聚合已有 MinerU API 实例：

```bash
mineru-router --host 0.0.0.0 --port 8002 \
  --local-gpus none \
  --upstream-url http://mineru-a:8000 \
  --upstream-url http://mineru-b:8000
```

Router 对外提供相同的 `/health` 和 `/tasks` 协议，因此 Comet-RAG 不需要切换
适配器。Router 和 worker 的任务映射都在内存中，服务重启后旧任务可能消失；
适配器遇到 404 会在同一总超时内重提一次，不会无限重提。

## 配置 Comet-RAG

`base_url` 指向 `mineru-api` 或 `mineru-router`，不是 vLLM：

```yaml
infrastructure_config:
  mineru:
    enabled: true
    base_url: http://127.0.0.1:8989
    backend: vlm-http-client       # 自带模型可使用 pipeline
    parse_method: auto             # auto | txt | ocr
    language: ch
    formula: true
    table: true
    connect_timeout_seconds: 10.0
    request_timeout_seconds: 60.0
    upload_timeout_seconds: 300.0
    poll_interval_seconds: 1.0
    parse_timeout_seconds: 900.0
    max_response_bytes: 16777216   # 16 MiB
    max_markdown_bytes: 8388608    # 8 MiB

limits:
  mineru_concurrency: 2
  mineru_queue: 16
  mineru_wait_timeout: 30.0
```

Comet-RAG 仅允许 `localhost`、`127.0.0.0/8` 和 `::1` 使用明文 HTTP；任何非回环
`base_url` 都必须使用 HTTPS，避免上传的原始 PDF 在网络路径中被读取或篡改。

`enabled=false` 时不会创建 MinerU 客户端，也不会注册 PDF Hook。启用后，组合根
创建 `AsyncMinerUDocumentExtractor` 并注册到当前 Context 的异步 HookRegistry，
应用关停时等待其连接池关闭。同步库调用使用独立的 `MinerUDocumentExtractor` 与同步注册表。

来源准入仍单独生效：本地文件需要 `allow_local` 与 `local_roots`，S3/MinIO 需要
`allow_s3`、bucket 白名单和连接配置；URL 仍受私网与重定向 SSRF 检查。后缀为
`.pdf` 只是候选格式，上传 MinerU 前还会按真实字节复验。

## 入库与观察

```bash
curl -X POST http://127.0.0.1:8000/kb \
  -H 'Content-Type: application/json' \
  -d '{"kb_id":"pdf-demo"}'

curl -X POST http://127.0.0.1:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{"kb_id":"pdf-demo","source":"/data/report.pdf"}'

curl http://127.0.0.1:8000/tasks/<task_id>

curl -X POST http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"kb_id":"pdf-demo","query":"报告结论","top_k":5}'
```

`GET /admin/limits` 中的 `mineru_gate` 给出 `limit`、`in_flight`、`waiting` 和
`rejected`。`in_flight` 长期贴顶说明解析服务是瓶颈；`waiting` 或 `rejected`
持续增长时，应优先扩 MinerU/Router 容量，再按上游能力调整本进程并发。

错误语义：429、5xx、网络错误和远端任务丢失可重试；确定性 4xx、协议不兼容、
伪造 PDF 与资源超限直接失败。取消 Comet-RAG 任务会停止本地轮询并释放响应流，
但 MinerU protocol v2 没有稳定的远端取消接口，不能承诺终止已经开始的 GPU 计算。

## 真实集成测试与基准

未设置地址或服务不可达时用例会 skip，不会让普通开发环境失败：

```bash
COMET_TEST_MINERU_URL=http://127.0.0.1:8989 \
COMET_TEST_MINERU_BACKEND=vlm-http-client \
uv run pytest -m integration tests/integration/test_mineru_e2e.py \
  --mineru-report mineru-report.json
```

2026-09-10 的 M2 验收使用 MinerU 3.4.5 / protocol v2 与远端 MinerU 2.5
vLLM：文本 PDF 端到端 2.10s，扫描 PDF 3.11s，均完成提取、分块、入库和检索；
Comet-RAG 测试进程 Python 堆峰值约 1.45 MB。

样本只有单页，只证明协议和全链路可工作，不能代表生产长文档的 p95/p99，也不能
测出外部 MinerU 的 CPU、显存或 RSS。因此保留 900s 总超时、16 MiB 响应和
8 MiB Markdown 的保守默认值，生产部署应使用自己的文档集重新采样。

2026-10-25 的[第二十八批实测](evaluations/mineru_pdf_20261025.md)补充了完整 7/80 页
公开 PDF、真实正文三页子集和重复句两页样本。两份完整 PDF 均成功解析，但均安全
回退 section；正文子集的原生页号 1–3 有效，另附到原文件物理页 5–7 的派生映射。
它不证明完整原文件已通过 page 验收。独立人工标签、固定模型工件和生产部署仍待验收。

## 不支持的集成方式

- 在 Comet-RAG 中 import `mineru` 或 `mineru_vl_utils`；
- 通过 Python SDK 直接调用 `MinerUClient`；
- 从 API 或 worker 启动 `mineru` CLI 子进程；
- 在 Comet-RAG 配置中管理模型权重或 GPU；
- 把 vLLM 地址直接当作 `mineru-api` 地址。

将来若要支持进程内 MinerU，必须新建规格重新评审依赖、进程隔离与资源治理，
不能作为 HTTP 适配器的隐式 fallback。

## 官方依据

- [MinerU 快速使用](https://github.com/opendatalab/MinerU/blob/mineru-3.4.5-released/docs/zh/usage/quick_usage.md)
- [MinerU FastAPI 实现](https://github.com/opendatalab/MinerU/blob/mineru-3.4.5-released/mineru/cli/fast_api.py)
- [MinerU 请求参数](https://github.com/opendatalab/MinerU/blob/mineru-3.4.5-released/mineru/cli/api_request.py)
- [MinerU API 客户端](https://github.com/opendatalab/MinerU/blob/mineru-3.4.5-released/mineru/cli/api_client.py)
