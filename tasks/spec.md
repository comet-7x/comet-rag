# Spec: Comet-RAG

> 状态：M1～M3 已完成；M4 Chunking 进入规格草案阶段（v1.3）
> 最后更新：2026-09-14
> 验收记录：M1 见 `tasks/plan.md` Checkpoint F；M2 见 `tasks/m2_spec.md` v1.0；M3 见 `tasks/m3_spec.md` v1.0；M4 草案见 `tasks/m4_spec.md`

---

## 0. 本规格锁定的假设

以下是与开发者确认过的前提，若有变化必须先改本文再改代码：

| # | 假设 | 来源 |
|---|---|---|
| A1 | 交付形态 = **库 + 参考服务**。`engines/` + `infrastructure/` 可 `pip install` 单独用；`api/` + `workers/` 是可直接跑的参考部署 | 已确认 |
| A2 | "生产端 / 消费端"取**消息队列语义**：生产端 = API 接请求建任务并投递；消费端 = Worker 拉任务执行 | 已确认 |
| A3 | 任务队列选 **ARQ**，不用 Celery。理由：ARQ async 原生，能跨任务复用 httpx 连接池；Celery 需 `asyncio.run()` 包裹，每任务重建连接池，直接伤害 S4 的资源利用目标 | 已确认 |
| A4 | 任务框架以 `poc/task_demo/task/` 为准，提升为 `comet_rag/tasks/`。`comet_rag/schemas/task.py` 作废删除 | 已确认 |
| A5 | 难改的现在留钩子（如租户维度），易加的以后再说；钩子后续不需要可随时删 | 已确认 |
| A6 | 任务状态存储选 **PostgreSQL**（已有 SQLAlchemy + Alembic），不引入 MongoDB。`TaskStore` 是 ABC，日后可换 | 已确认 |
| A7 | Python ≥ 3.12，async-first，模型侧统一走 OpenAI 兼容协议打自建 Qwen3-VL 服务 | 现有代码 |
| A8 | **首个里程碑 M1 = 打通 DOCX 全链路**。MinerU / PDF 是独立的后续里程碑 M2 | 已确认 |
| A9 | 向量库**只实现 Milvus**，不做多后端兼容。但配一个 `InMemoryVectorStore`（测试必需，顺带验证抽象），两者跑同一套契约测试 | 已确认 |
| A10 | **移除确认门**（`AWAITING_REVIEW` 及 `review_*` 字段）。理由：确认门的价值在于"中间态昂贵不可丢"，而 DOCX 链路的中间态（chunks）重算只要几秒，重算比挂起便宜。**但断点续跑要保留**——重试要从失败阶段续跑，不是从头重来 | 已确认 |
| A10-修正 | ⚠️ **原 A10 基于一个错误前提**。T4 实测（2026-08-09）证明：断点续跑目前**只由确认门驱动**——`resume_stage` 仅在 `NeedsReview` 时被赋值，`executor._mark_failed` 走可重试分支时不设它。多阶段流水线在可重试失败后是**从头全量重跑**（实测访问序列 `s1,s2,s3,s1,s2,s3`）。因此"保留断点续跑"不是保留，而是**新实现**：需改为由失败驱动。T5 范围相应扩大 | 已修正 |
| A11 | Milvus collection schema **预留 sparse vector 字段**，M1 不写入、不实现混合检索逻辑（留给 M3）。理由：sparse 字段必须建表时声明，事后添加需全量重灌 | 已确认 |
| A12 | 知识库建 `knowledge_bases` 表（不是纯字符串标签），**必须含 `embedding_model` 与 `embedding_dim` 字段** | 已确认 |
| A13 | M2 **只连接外部 `mineru-api` / `mineru-router`**。Comet-RAG 不安装、不导入、不启动 MinerU SDK、Torch、模型权重或 GPU 运行时 | 已确认 |

---

## 1. Objective

**Comet-RAG 是一个通用型高并发 RAG 框架，输入任意文件，输出结构化块或向量检索结果。**

它同时是两样东西：

1. **一个库** —— 别人可以只用你的 docx 解析器，或只用你的分块器，不必接受整套架构。
2. **一套参考服务** —— 开箱可部署的 FastAPI + ARQ Worker，证明这个库能撑住真实负载。

这个双重定位推导出**全规格最硬的一条约束**：

> `comet_rag/engines/` 不得 import 任何基础设施（Redis、Postgres、Milvus、S3、ARQ、FastAPI）。

违反它，"库"这一半就不成立了——用户为了跑一个 docx 解析要装一整套中间件。

### 目标用户

- **库用户**：想在自己项目里复用某个模块（解析、分块、检索）的 Python 开发者
- **服务用户**：想直接部署一套 RAG 后端的团队

### 里程碑

| | 范围 | 出口标准 |
|---|---|---|
| **M1** | **DOCX 全链路** —— 上传 docx → 解析 → 分块 → 向量化 → 入 Milvus → 检索命中 | §8 的 S1–S5 全绿（已完成） |
| **M2** | **PDF 支持（通过 HTTP 连接外部 MinerU 服务）** | 本地、URL、S3 PDF 复用 M1 入库链路；默认安装不含 MinerU 运行时（已完成） |
| **M3** | **混合检索（Milvus BM25 + RRF）** | 两路召回、纯 RRF、通道降级与真实 Milvus 链路全绿（已完成） |
| **M4** | **可追溯 Chunking + 可选父子索引** | 带位置的块、结构感知切分、单链路装配；父子索引通过独立 schema 决策门 |

M1、M2、M2 后 P1 与 M3 已完成并合入 `develop`。仓库结构归一化和跨格式文档
规范化也已由 PR #56 合入。当前在 `feature/m4-chunking` 上执行 M4-T1；先确认并冻结
Chunking 规格，再修改代码。父子索引会新增持久化表和 metadata 约定，必须经过单独
决策门。

### 非目标（明确不做）

- 前端 UI / 管理后台
- 模型训练、微调
- 完整的权限系统（RBAC、OAuth、配额）
- 多语言 SDK
- **多向量库后端兼容**（见 A9）
- **人工确认门**（见 A10）

---

## 2. Tech Stack

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | Python ≥ 3.12 | 已用 `type` 语句、`StrEnum`、PEP 695 |
| Web | FastAPI + uvicorn | 已有 |
| 校验 | Pydantic v2 | API 出入参、配置 |
| 内部数据 | dataclass（`slots=True`） | 引擎内部与 Task，避免 Pydantic 校验开销 |
| 任务队列 | **ARQ** | `server` extra |
| 任务状态 | PostgreSQL + SQLAlchemy 2.0 + Alembic | `server` extra |
| 向量库 | Milvus（pymilvus） | `milvus` extra |
| 对象存储 | S3 兼容（aioboto3） | `server` extra |
| 文档提取 | 外部 `mineru-api` / `mineru-router`（httpx） | M2；Comet-RAG 不嵌入 MinerU SDK |
| 模型 | OpenAI 兼容协议（openai SDK / httpx） | 核心依赖 |
| 日志 | loguru | 核心依赖 |
| 测试 | pytest + pytest-asyncio + pytest-cov | `dev` dependency group |
| Lint | ruff + pyright | `dev` dependency group |
| 提交 | commitizen + pre-commit | `dev` dependency group |

M1 所需依赖均已落入对应分组；M2 通过已有的 httpx 连接外部 MinerU 服务，
不提供 `mineru` extra。

### 依赖分组（对应 A1）

```toml
[project]
dependencies = [...]          # 仅 engines 所需：pydantic, httpx, loguru, magika, python-docx, lxml, openai

[project.optional-dependencies]
milvus  = ["pymilvus>=2.5"]
server  = ["fastapi", "uvicorn[standard]", "sqlalchemy", "alembic", "asyncpg", "arq", "aioboto3"]
all     = ["comet-rag[milvus,server]"]
```

`pip install comet-rag` 只装库；`pip install comet-rag[server]` 才拉基础设施。**这条是 A1 的强制执行手段。**

---

## 3. Commands

```bash
# 环境
uv sync --all-groups                      # 安装全部依赖（含 dev）
uv sync --no-dev                          # 仅运行时依赖

# 质量门
uv run ruff check --fix .                 # lint + 自动修复
uv run ruff format .                      # 格式化
uv run pytest                             # 全部测试
uv run pytest tests/unit -q               # 仅单元测试（无外部依赖，秒级）
uv run pytest -m integration              # 需要 docker-compose 起中间件
uv run pytest --cov=comet_rag --cov-report=term-missing

# 运行
uv run uvicorn comet_rag.api.main:app --reload --port 8000
uv run arq comet_rag.workers.preprocessor.WorkerSettings
uv run arq comet_rag.workers.embedder.WorkerSettings

# 基础设施
docker compose up -d                      # postgres + redis + milvus + minio
uv run alembic upgrade head               # 数据库迁移
uv run alembic revision --autogenerate -m "..."

# 基准
uv run pytest tests/benchmark --benchmark-only
```

---

## 4. Project Structure

```
comet_rag/
├── engines/              ★ 库核心 —— 禁止 import 任何基础设施
│   ├── documents/        文档格式内聚实现 + 跨格式规范化
│   ├── chunkers/         文本 → chunks
│   ├── embedding/        后端无关的批量排程
│   ├── retrieval/        后端无关的检索算法
│   └── pipelines/        Hook 与 Pipeline 值对象
│
├── ports/                ★ 跨层契约与值对象
├── infrastructure/       ★ 外部系统适配器
│   ├── providers/        embedding / reranker / vision / llm / ocr
│   ├── loaders/          S3 兼容对象存储加载器
│   ├── vectorstore/      BaseVectorStore ABC + MilvusStore
│   └── database/         SQLAlchemy 模型、仓储与 session
│
├── tasks/                ★ 通用任务框架
│   ├── models.py         Task / TaskStatus / TaskError / TaskEvent / StageRecord
│   ├── states.py         状态机迁移表（唯一合法迁移来源）
│   ├── store*.py         TaskStore ABC + 内存/PostgreSQL 实现
│   ├── executor*.py      TaskExecutor ABC + 进程内/ARQ 实现
│   ├── runner.py         Runner Protocol + kind 注册表 + StagePipeline
│   └── service.py        TaskService（store + executor 门面）
│
├── services/             ★ 业务编排 —— 把 engines 拼成业务用例
│   ├── ingestion.py      入库用例：注册为 kind="ingest" 的 runner
│   ├── retrieval.py      检索用例：query → 向量召回 → rerank
│   └── knowledge_base.py 知识库 CRUD
│
├── workers/              ★ 消费端入口
│   ├── preprocessor.py   CPU 密集：解析 + 清洗 + 分块
│   └── embedder.py       IO 密集：向量化 + 写入向量库
│
├── api/                  ★ 生产端入口
│   ├── main.py  deps.py  lifespan.py  middleware.py
│   └── routes/           search / ingest / tasks / kb / admin
│
├── composition/          唯一组合根与应用上下文
├── schemas/              API 出入参（Pydantic）
├── config/               YAML 加载 + Pydantic 校验
├── core/                 并发、降级、日志、trace 与时间
└── exceptions/           异常层次

tests/
├── unit/                 无外部依赖，必须秒级跑完
├── integration/          需 docker-compose，标记 @pytest.mark.integration
├── e2e/                  完整链路：上传 → 入库 → 检索
├── benchmark/            性能基线
└── fixtures/             测试文件样本（docx/pdf/...）

poc/                      实验场，不纳入打包，不要求测试覆盖
docs/                     使用文档
tasks/                    本规格 + plan.md + todo.md（不打包）
```

### 依赖方向（单向，禁止反向）

```
api ──┐
      ├──► services ──► engines ──► （无依赖）
workers ──┘     │
                └──► infrastructure ──► 外部中间件
      tasks ◄───┘（services 用 tasks，tasks 不知道 RAG 业务）
```

`tasks/` 是**产品无关**的通用任务框架（POC 已经做到了这点，`kind` 字段就是解耦手段）。它不许 import `engines/` 或 `services/`。

---

## 5. Code Style

沿用现有风格，不做改动。基准示例：

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class Chunk:
    """引擎内部数据用 dataclass + slots：省内存，且拼错字段名当场 AttributeError。"""

    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None


class BaseEmbeddingModel(Protocol):
    """跨模块边界的契约优先用 Protocol —— 不逼实现方 import 并继承。

    仅当基类需要提供共享实现（如 TaskStore 的模板方法）时才用 ABC。
    """

    async def aembed_query(self, query: str, /) -> list[float]: ...
    async def aembed_batch(self, documents: Sequence[str], /) -> list[list[float]]: ...

    #: 一次请求最多装几篇；发几个请求、几个并发由调用方排程
    batch_limit: int
```

**约定**：

- 所有模块首行 `from __future__ import annotations`
- 公开 API 全量类型标注；`Any` 需在旁注明理由
- 异步方法以 `a` 前缀区分同步版本（`run`/`arun`、`embed`/`aembed`）——现有约定，保持
- 重依赖在函数内 import（现有 `hooks.py` 的做法），保证 `import comet_rag` 轻量
- 注释只补代码无法表达的契约与原因，详见 `docs/comment_style.md`
- 中文注释与 docstring 保持简短一致

---

## 6. Testing Strategy

M1 已建立单元、契约、集成、端到端与基准测试；默认单元测试必须保持在 10 秒内。

### 分层

| 层 | 位置 | 依赖 | 要求 |
|---|---|---|---|
| 单元 | `tests/unit/` | 无。模型/存储全部用 fake | 全套 < 10s；覆盖率 ≥ 70% |
| 集成 | `tests/integration/` | docker-compose 起 pg/redis/milvus/minio | 每个 infrastructure 实现至少一套 |
| 端到端 | `tests/e2e/` | 全栈 | 至少 1 条：提交 → 轮询 → 检索命中 |
| 基准 | `tests/benchmark/` | 全栈 | 见 §8 S4 |

### M1 补测顺序（按"出 bug 的代价"排序，已完成）

1. **`tasks/states.py` 状态机** —— 纯函数、零依赖、bug 后果最严重（"已取消的任务又变成成功"）。参数化把 7×7 迁移矩阵全覆盖。
2. **`tasks/store.py` 乐观锁与租约** —— `InMemoryTaskStore` 天然可测。重点：并发 CAS 冲突、`heartbeat` 的 `bump=False` 不涨版本、`sweep_stale` 回收逻辑。
3. **`engines/chunkers/`** —— 纯函数，输入输出明确，边界条件多（空文本、超长无分隔符、overlap ≥ size）。
4. **`engines/documents/docx/parser.py`** —— DOCX 解析核心。用生成的真实 docx 样本做快照测试。
5. **`services/`** —— 用 fake 模型 + `InMemoryTaskStore` 测编排逻辑。

### 关键 fixture

```python
# tests/conftest.py
@pytest.fixture
def store() -> InMemoryTaskStore: ...


@pytest.fixture
def fake_embedding_model() -> BaseEmbeddingModel:
    """返回确定性伪向量，不打网络。断言批量调用次数以验证 S4-3。"""
```

**硬性要求**：不允许任何单元测试打真实网络或需要 GPU 服务。

---

## 7. Boundaries

### Always（每次都要做）

- 提交前跑 `ruff check` 与 `pytest tests/unit`
- 新增公开 API 必须有类型标注与 docstring
- 改 `Task` 状态一律走 `store.transition()`，不允许裸赋值 `task.status = X`
- 队列里只传 `task_id`，任务数据一律从 `TaskStore` 读
- 对模型服务的调用一律经过并发闸门，不允许裸调
- 遵循 commitizen 约定式提交

### Ask first（先问再动）

- 给 `engines/` 增加任何第三方依赖（可能破坏 A1）
- 修改 `Task` 的持久化字段（涉及数据库迁移）
- 修改向量库 schema / metadata 字段（**可能需要全量重灌**）
- 变更 `TaskStore` / `TaskExecutor` / `BaseVectorStore` 的抽象接口
- 引入新的中间件依赖
- 新增第三个 `BaseVectorStore` 实现（A9 明确只做 Milvus + InMemory）

### Never（不允许）

- 在 `engines/` 里 import Redis / Postgres / Milvus / S3 / ARQ / FastAPI
- 提交密钥。配置走 YAML + 环境变量，`config.yaml` 进 `.gitignore`
- 在循环里逐条调 embedding（必须走 `engines/embedding/batch.py` 的批量排程）
- 在函数内部 `httpx.AsyncClient()` 现建现销（必须复用应用级实例）
- 无界队列 / 无上限并发
- 让 Milvus 专有语法（表达式字符串、consistency level）穿透 `BaseVectorStore` 接口
- 删除失败的测试来让 CI 变绿

---

## 8. Success Criteria

### S1 — 库与服务真正分离（对应 A1）

- [x] 在干净虚拟环境里 `pip install comet-rag`（不带 extras），`from comet_rag.pipeline import Pipeline` 能成功导入并解析一个 docx
- [x] CI 中有一个 job 只装基础依赖验证库可独立导入和解析，通过
- [x] AST 分层守卫确认 `engines/` 不依赖任何基础设施包

### S2 — 任务框架落地

- [x] `poc/task_demo/task/` 提升为 `comet_rag/tasks/`，`Pipeline` 更名 `StagePipeline`（避开与 `engines/pipelines` 撞名）
- [x] 旧任务领域 schema 已删除；任务 HTTP DTO 位于 `comet_rag/api/schemas/task.py`
- [x] 确认门已按 A10 移除，且 `resume_stage` / `context` 续跑仍工作：
      *验证*：让 runner 在第 3 阶段抛 `RetriableError`，重试后应从第 3 阶段开始，而非第 1 阶段
- [x] Postgres 中 `tasks.status` 为 **varchar** 而非 PG 原生 enum（保留将来加状态值的零成本可逆性）
- [x] `PostgresTaskStore` 通过与 `InMemoryTaskStore` **完全相同**的一套契约测试
- [x] `ArqExecutor` 通过与 `InProcessExecutor` 相同的执行器契约测试
- [x] 杀死 worker 进程后，`sweep_stale` 能在租约超时内把任务退回 PENDING 并被另一 worker 接管

### S3 — 端到端链路打通

- [x] `POST /ingest` 提交文件 → 返回 `task_id` → 轮询见到 stage 依次推进 → `SUCCEEDED`
- [x] `POST /search` 能检索到上一步入库的内容，返回带 `score` 的排序结果
- [x] 每个 chunk 的 metadata 携带 `kb_id`；Milvus 使用“一 KB 一 collection”隔离
- [x] 同一文件带相同 `idempotency_key` 重复提交，不产生第二个任务
- [x] `knowledge_bases` 表记录 `embedding_model` / `embedding_dim`；模型或维度不一致时拒绝写入（A12）
- [x] Milvus collection schema 含 sparse vector 字段（M1 不写入）（A11）
- [x] `MilvusStore` 与 `InMemoryVectorStore` 通过**同一套契约测试**（A9）
- [x] `BaseVectorStore.asearch` 的 `filter` 参数是结构化 `dict`，不暴露 Milvus 表达式字符串
- [x] 契约测试覆盖“写入后立即查询”，Milvus 使用 `Session` 一致性保证可见

### S4 — 资源自适应（性能目标）

不设 QPS 数字（当前无真实负载，编造数字无意义），改为六条可验证的架构约束：

1. [x] **全链路背压** —— 所有队列有界。压测下应表现为拒绝或阻塞，**不得 OOM**。
   *验证*：投递量 10× 于处理能力，进程存活，内存不随投递量线性增长。
2. [x] **并发闸门** —— 对模型服务的并发上限可配置且被强制执行。
   *验证*：配 `max_concurrency=4`，用记录并发峰值的 fake 模型断言峰值 ≤ 4。
3. [x] **并发优先** —— embedding 一律窗口化并发，不得逐条串行等待。
   *验证*：`astream_run` 处理 200 chunk 的文档，fake 模型记录的并发峰值 > 1 且 ≤ `max_concurrency`；首个 chunk 产出时已 embed 数 ≤ `embed_batch_size`（流式语义未退化）。
   > 调度统一收口到 `engines/embedding/batch.py`：模型声明单请求 `batch_limit`，
   > 调用方决定窗口与并发。支持原生批量的适配器合并请求，不支持的适配器有界扇出。
4. [x] **连接池复用** —— httpx client 由适配器实例持有或从应用生命周期注入。
   *验证*：`grep -rn "AsyncClient(" comet_rag/` 的结果，要么接受外部注入，要么位于生命周期管理代码中。
   > URL 与模型适配器均复用实例级客户端；注入的客户端归调用方，自建客户端由适配器关闭。
5. [x] **分级降级** —— 资源紧张时按序降级：先关 rerank → 再降 top_k → 最后拒绝新任务。降级动作必须打日志。
   *验证*：注入模型服务超时，检索仍返回结果（无 rerank）且日志有降级记录。
6. [x] **基准可测** —— `tests/benchmark/` 能产出当前基线；涉及性能的 PR 须附前后对比。

### S5 — 可维护性

- [x] `tests/unit` 覆盖率 ≥ 70%，`comet_rag/tasks/` ≥ 90%（三层合并口径）
- [x] pre-commit 钩子与 CI 质量门已配置
- [x] README、`docs/` 与公开 API 由文档示例测试持续校验

---

## 9. M1 问题关闭记录

| # | 处理结果 |
|---|---|
| P1 | 四种 Pipeline 入口统一走窗口化 embedding 排程 |
| P2 | `pipeline_usage.md` 已与真实签名对齐，并由文档示例测试防腐 |
| P2b | URLLoader 改为复用实例级同步/异步客户端并明确所有权 |
| P3 | 配置类已接入组合根，未使用的 Mongo 配置已移除 |
| P4 | `composition.Context`、lifespan 与逆序资源清理已落地 |
| P5 | `/search` 已接入召回、可选重排与失败降级 |
| P6 | `MilvusStore` 与 `InMemoryVectorStore` 已实现并共享契约测试 |
| P7 | API、workers、services、providers 与 database 均已实现并完成装配 |
| P8 | 测试夹具会快照并恢复 `PipelineHooks`，用例间不再污染 |

---

## 10. Open Questions

### 已裁决（记录理由，便于日后回溯）

| 原问题 | 结论 | 理由 |
|---|---|---|
| 任务状态存哪 | PostgreSQL（A6） | 已有 SQLAlchemy + Alembic；任务表是强 schema；为一张表引入 Mongo 不划算。`TaskStore` 是 ABC，可换 |
| MinerU/PDF 优先级 | 独立里程碑 M2（A8） | 先把 DOCX 一条链路做扎实 |
| MinerU 部署方式 | 只连接外部 `mineru-api` / `mineru-router`（A13） | 保持默认安装轻量；解析服务独立扩容，避免把 Torch 与模型权重带进 Comet-RAG 进程 |
| 多向量库兼容 | 只做 Milvus + InMemory（A9） | 过早抽象会拉到最小公分母；第二个实现（内存版）因测试需要本就要写，顺带验证抽象 |
| 确认门去留 | 移除（A10） | 中间态重算便宜（解析 docx 几秒），重算比挂起划算。属纯代码逻辑、无数据迁移，将来需要可低成本加回 |

| 混合检索是否进 M1 | schema **预留 sparse 字段**，逻辑放 M3（A11） | 字段必须建表时声明，事后加需全量重灌；预留成本近零，M3 变成纯加代码 |
| KB 是标签还是表 | 建 `knowledge_bases` 表（A12） | 必须记录 embedding 模型版本，否则换模型后新旧向量混于同一空间，检索**静默劣化**且无法分辨该重算哪些 chunk |

### 仍待决策

（暂无。新问题在此追加。）
