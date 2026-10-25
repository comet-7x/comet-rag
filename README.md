# Comet-RAG

一个通用的 RAG 框架，**既是库也是服务**。

- **当库用**：只装核心依赖，拿 `engines/` 做文档解析与切分，不碰任何中间件。
- **当服务用**：在它之上加任务调度、持久化与 HTTP 接口，跨进程可扩容、崩溃可恢复。

两者共用同一份解析代码。这个双重定位由 `pyproject.toml` 的依赖分组
**在安装期强制**，并由 AST 层级守卫在 CI 里盯着（`tests/unit/test_layering.py`）。

> 当前进度（2026-10-25）：V2 第二十三批补齐独立进程 worker 信号关停/恢复与单测预算复核。
> M4 已有结构感知分块、DocumentStore、激活/GC、revision-aware 读取及 opt-in 生产接线；
> 默认仍为 legacy，实际资料迁移、部署切换/回退及完整 H/I 尚未完成。见 [下一步计划](tasks/v2_next_steps.md)。
> 第十七/十八批补齐独立发布/清理恢复、迟到写补偿、游标续页与应用删除保护。
> 第十九批在外部写入前持久化准备意图，并交付默认关闭的 IO worker 定时对账及告警信号；
> 不自动切换生产入库或开启 revision 检索。生产接线范围及门禁见
> [对账设计](tasks/v2_reconciliation_design.md)，当前入库路径保持不变。
> 第二十批单列历史缺载荷诊断，新增 API 入库准入暂停；已选择新命名空间重建后核验切换，
> 第二十一批复用五阶段协议并验证跨道、发布与 GC 重试；第二十二批提供 `revision-preflight`，
> 第二十三批用真实 SIGTERM/SIGKILL 验证 revision 续跑，worker 默认先给在途工作 30 秒排干机会；
> [迁移配置与验收](docs/revision_migration.md)说明启用条件，不表示已迁移用户数据或切换生产。

| 你要做什么 | 从这里开始 |
|---|---|
| 只解析、分块或扩展 Pipeline | [库使用说明](docs/pipeline_usage.md) |
| 启动 API/worker、配置模型和中间件 | [部署与配置](docs/deployment.md) |
| 理解边界、调用路径与当前切片 | [架构](docs/architecture.md)、[流程图](docs/structure.md) |
| 修改代码或贡献实现 | [开发规范](CONTRIBUTING.md)、[测试说明](tests/README.md) |

---

## 一、当库用

```bash
pip install .               # 在仓库根目录，仅安装核心依赖
```

```python
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.pipeline import Pipeline

with Pipeline(config=PipelineConfig(chunk_size=2000, chunk_overlap=200)) as pipeline:
    result = pipeline.run("报告.docx")

print(f"共 {len(result.chunks)} 个 chunk")
for chunk in result.chunks:
    print(chunk.text[:100])
```

没有 Redis、没有 Postgres、没有 FastAPI。PDF 也能通过 Pipeline 使用，但必须显式
连接外部 MinerU；详见 [docs/pipeline_usage.md](docs/pipeline_usage.md)。

---

## 二、当服务用

### 最快一条路（不需要任何中间件）

```bash
uv sync --extra server
cp config.example.yaml config.yaml      # 改掉 embedding_model 的 base_url 与 dim
```

该方式不需要数据库或队列服务，**仍需要可用的 Embedding HTTP 服务**；内存内容在进程
重启后丢失。未部署 reranker 时删除示例中的该配置或设为 null。

下面用服务器本地文件演示。先在已有 config.yaml 中修改这些来源策略字段，把目录替换
为实际存在的绝对路径，并将报告放入其中；默认配置禁止本地路径入库。

```yaml
ingest_policy:
  allow_local: true
  local_roots: ["/data/corpus"]
```

```bash
uv run comet-rag serve
```

```bash
curl -X POST localhost:8000/kb     -d '{"kb_id":"demo"}'                          -H 'Content-Type: application/json'
curl -X POST localhost:8000/ingest -d '{"kb_id":"demo","source":"/data/corpus/报告.docx"}' -H 'Content-Type: application/json'
curl      localhost:8000/tasks/TASK_ID
curl -X POST localhost:8000/search -d '{"kb_id":"demo","query":"关键结论是什么"}'  -H 'Content-Type: application/json'
```

将 TASK_ID 替换为返回的 task_id；等任务成功后再检索。source 是服务器可读取的路径，不是上传
客户端电脑中的文件。URL/S3 来源、权限策略及失败诊断见部署说明。

检索默认仍是向量召回；需要精确术语或混合召回时显式选择模式：

```bash
curl -X POST localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"kb_id":"demo","query":"TraitObject 内存布局","mode":"hybrid","top_k":5}'
```

`mode` 可选 `dense`、`keyword`、`hybrid`。响应里的 `channels` 表示实际执行的
召回通道；hybrid 单路故障时会退到仍可用的一路，并在 `degradations` 给出安全诊断。

### 生产形态

先按部署说明将后端设为 `milvus`、`postgres`、`arq` 并填写连接配置，再安装全部 extras。
仅启动容器不会自动切换 config.yaml 中的内存后端。以下仍是已交付入库路径的部署方式，
不代表默认关闭的 revision 生产路径已启用或用户数据已迁移。

```bash
uv sync --extra all
docker compose up -d && uv run alembic upgrade head
uv run comet-rag serve                  # 生产端
uv run comet-rag worker preprocessor    # CPU 密集：解析 / 分块
uv run comet-rag worker embedder        # IO 密集：向量化 / 写库
```

**两个 worker 都要起**，理由与其余细节见 [docs/deployment.md](docs/deployment.md)。

---

## 设计上比较特别的几点

**队列里只放 `task_id`。** 任务数据一律回库读，于是重试、崩溃恢复、断点续跑、
跨 worker 移交全都退化成同一个动作 `submit(task_id)`。

**worker 按负载特征分，不按业务名词分。** preprocessor 靠加进程扩、embedder
靠加并发扩 —— 反过来会真的更慢，且症状是"上游变慢了"，很难联想到自己身上。

**并发闸门做在适配器基类的模板方法里。** 同步与异步模型分别使用 `Foo` / `AsyncFoo`，同名 `embed()` / `embed_media()` /
`embed_batch()` 都是 `@final`，子类只实现内部 `_embed()` 等扩展点，所以
"绕过闸门"在类型层面就没有写法。换成"包一层 wrapper"只是约定 —— 谁直接调
底层都能绕过去且不报错，而闸门恰恰是静默失效型的保护。

**核心抽象各有一套与实现无关的契约测试**。`InMemoryTaskStore` 与
`PostgresTaskStore`、`InProcessExecutor` 与 `ArqExecutor`、`AsyncInMemoryVectorStore`
与 `AsyncMilvusStore`、`InMemoryDocumentStore` 与 `PostgresDocumentStore` 分别跑同一套断言；
关键词召回也由两种向量库实现共享同一套契约 —— "换后端行为不变"这句话靠断言兑现，
不靠文档。

V2 的 Python API 按执行模式分离；旧 `a*` 入口不再保留，迁移方式见
[版本说明](docs/release_notes.md) 和 [模型用法](docs/model_usage.md)。

更多见 [docs/architecture.md](docs/architecture.md)；目录结构与流程图见
[docs/structure.md](docs/structure.md)；升级中的破坏性变更见
[docs/release_notes.md](docs/release_notes.md)。

---

## 开发

```bash
make install

uv run pytest                    # 单元测试，零外部服务依赖，目标 < 10s
uv run pytest -m e2e             # 端到端，全内存
uv run pytest -m integration     # 需要 docker compose up -d
uv run pytest -m benchmark       # 性能与离线检索回归（见 docs/benchmark.md）

make lint
make format
```

**中间件没起时集成测试会 skip，不会 fail** —— 让它们红一片只会训练出"看到红色
就忽略"的习惯，那比没有测试更糟。

测试的组织方式、契约怎么写、快照怎么更新，见 [tests/README.md](tests/README.md)。
可读性与职责边界见 [CONTRIBUTING.md](CONTRIBUTING.md)；单测耗时波动及历史验收见
[V2 实施记录](tasks/v2_plan.md)。

## 文档

|                                                          |                                      |
| -------------------------------------------------------- | ------------------------------------ |
| [architecture.md](docs/architecture.md)                   | 分层、核心抽象、两种部署形态         |
| [structure.md](docs/structure.md)                         | 目录结构、依赖图、入库/检索流程图    |
| [交互架构图](docs/diagrams/comet-rag.html)                 | 离线浏览；图源与更新步骤见 [维护说明](docs/diagrams/README.md) |
| [model_usage.md](docs/model_usage.md)                     | Embedding、图片与 Reranker 用法      |
| [deployment.md](docs/deployment.md)                       | 配置、扩容、运维、排查               |
| [benchmark.md](docs/benchmark.md)                         | 性能基线与它**不能**回答的问题 |
| [retrieval_evaluation.md](docs/retrieval_evaluation.md)   | 固定语料、检索标签、评分规则与模型实测入口 |
| [pipeline_usage.md](docs/pipeline_usage.md)               | 只当库用时看这个                     |
| [docx_parser_internals.md](docs/docx_parser_internals.md) | docx 解析内幕                        |
| [mineru_integration.md](docs/mineru_integration.md)       | PDF / MinerU 部署、配置与验收        |
| [comment_style.md](docs/comment_style.md)                 | 注释与 Docstring 的编写、评审标准    |
| [CONTRIBUTING.md](CONTRIBUTING.md)                         | 开发规范、质量门与变更边界          |
| [v2_next_steps.md](tasks/v2_next_steps.md)                 | 当前剩余项、后续批次与验收条件      |
| [v2_ecosystem_review.md](tasks/v2_ecosystem_review.md)     | skills / RAG 工具链调研及适用判断   |
