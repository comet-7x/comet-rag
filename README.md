# Comet-RAG

一个通用的 RAG 框架，**既是库也是服务**。

- **当库用**：只装核心依赖，拿 `engines/` 做文档解析与切分，不碰任何中间件。
- **当服务用**：在它之上加任务调度、持久化与 HTTP 接口，跨进程可扩容、崩溃可恢复。

两者共用同一份解析代码。这个双重定位由 `pyproject.toml` 的依赖分组
**在安装期强制**，并由 AST 层级守卫在 CI 里盯着（`tests/unit/test_layering.py`）。

> 当前进度：M1（DOCX 全链路）。PDF / MinerU 是 M2，混合检索是 M3。

---

## 一、当库用

```bash
pip install comet-rag        # 只装 pydantic / httpx / lxml / python-docx 一类
```

```python
from comet_rag.engines.pipelines import Pipeline, PipelineConfig

pipeline = Pipeline(config=PipelineConfig(chunk_size=2000, chunk_overlap=200))
result = pipeline.run("报告.docx")

print(f"共 {len(result.chunks)} 个 chunk")
for chunk in result.chunks:
    print(chunk.text[:100])
```

没有 Redis、没有 Postgres、没有 FastAPI。详见 [docs/pipeline_usage.md](docs/pipeline_usage.md)。

---

## 二、当服务用

### 最快一条路（不需要任何中间件）

```bash
uv sync --extra server
cp config.example.yaml config.yaml      # 改掉 embedding_model 的 base_url 与 dim
uv run comet-rag serve
```

```bash
curl -X POST localhost:8000/kb     -d '{"kb_id":"demo"}'                          -H 'Content-Type: application/json'
curl -X POST localhost:8000/ingest -d '{"kb_id":"demo","source":"报告.docx"}'      -H 'Content-Type: application/json'
curl      localhost:8000/tasks/<task_id>
curl -X POST localhost:8000/search -d '{"kb_id":"demo","query":"关键结论是什么"}'  -H 'Content-Type: application/json'
```

### 生产形态

```bash
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

**并发闸门做在基类的模板方法里。** `aembed()` 不可覆写，子类只能实现
`_aembed()`，所以"绕过闸门"在结构上做不到。包装器只是约定，会被绕过且不报错。

**三个核心抽象各有一套与实现无关的契约测试**（79 条）。`InMemoryTaskStore` 与
`PostgresTaskStore`、`InProcessExecutor` 与 `ArqExecutor`、`InMemoryVectorStore`
与 `MilvusStore` 跑同一套断言 —— "换后端行为不变"这句话靠它们兑现，不靠文档。

更多见 [docs/architecture.md](docs/architecture.md)。

---

## 开发

```bash
uv sync --all-extras

uv run pytest                    # 单元测试，零依赖，~10s
uv run pytest -m e2e             # 端到端，全内存
uv run pytest -m integration     # 需要 docker compose up -d
uv run pytest -m benchmark       # 性能基线（见 docs/benchmark.md）

uv run ruff check && uv run ruff format
```

**中间件没起时集成测试会 skip，不会 fail** —— 让它们红一片只会训练出"看到红色
就忽略"的习惯，那比没有测试更糟。

测试的组织方式、契约怎么写、快照怎么更新，见 [tests/README.md](tests/README.md)。

## 文档

| | |
|---|---|
| [architecture.md](docs/architecture.md) | 分层、核心抽象、两种部署形态 |
| [deployment.md](docs/deployment.md) | 配置、扩容、运维、排查 |
| [benchmark.md](docs/benchmark.md) | 性能基线与它**不能**回答的问题 |
| [pipeline_usage.md](docs/pipeline_usage.md) | 只当库用时看这个 |
| [docx_parser_internals.md](docs/docx_parser_internals.md) | docx 解析内幕 |
| [mineru_integration.md](docs/mineru_integration.md) | MinerU 集成（M2） |
