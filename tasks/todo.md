# TODO: Comet-RAG M1

> 依据 `tasks/plan.md`。每项完成后勾选并跑该阶段的 Checkpoint。
> 规模：XS=1 文件 / S=1-2 / M=3-5。**无 L 及以上任务**——出现即需再拆。

---

## Phase 0：地基

### ✅ T1 — 测试基建与 CI 骨架

**描述：** 当前 6600 行代码零测试。先建保护网，否则后续每一步改动都是盲改。

**验收标准：**
- [x] `pyproject.toml` dev 组含 `pytest-asyncio`、`pytest-cov`；配置 `asyncio_mode = "auto"`
- [x] `tests/` 目录结构建立：`unit/ integration/ e2e/ benchmark/ fixtures/`，`conftest.py` 就位
- [x] `pytest.ini_options` 注册 `integration` / `e2e` / `benchmark` 三个 marker，默认只跑 `unit`
- [x] 至少 1 个真实断言的冒烟测试（如 `TextChunker` 切一段文本）

**验证：**
- [x] `uv run pytest tests/unit -q` 通过
- [x] `uv run pytest -m integration` 在无 docker 时**跳过**而非报错

**依赖：** 无
**文件：** `pyproject.toml`、`tests/conftest.py`、`tests/unit/test_smoke.py`
**规模：** S

---

### ✅ T2 — pyproject 依赖分组，强制执行 A1

**描述：** 把依赖拆成核心 / milvus / server 三组，让"`engines` 不依赖基础设施"从口头约定变成安装期强制。

**验收标准：**
- [x] `[project].dependencies` 只留 engines 所需：pydantic、httpx、loguru、magika、python-docx、lxml、openai、pyyaml
- [x] fastapi、uvicorn、sqlalchemy、alembic 移入 `optional-dependencies.server`
- [x] 新增 `optional-dependencies.milvus`、`.all`
- [x] 新增 CI job：只装核心依赖跑 `tests/unit`

**验证：**
- [x] `grep -rE "import (redis|pymilvus|sqlalchemy|arq|fastapi|aioboto3)" comet_rag/engines/` 无输出
- [x] 干净 venv 中 `pip install -e .` 后 `python -c "from comet_rag.engines.pipelines import Pipeline"` 成功

**依赖：** T1
**文件：** `pyproject.toml`、`.github/workflows/ci.yml`
**规模：** S

---

### ✅ T3 — 修复 `docs/pipeline_usage.md`

**描述：** 该文档写于 hooks 重构之前，**示例代码照抄会直接报错**。要开源，这是第一批用户的第一印象。

**验收标准：**
- [x] hook 签名更正为 `(LoaderContent, PipelineConfig) -> str` 与 `(str, PipelineConfig) -> list[str]`
- [x] `clean_to_string` → `clean_to_markdown`（该方法不存在，实际只有 `clean_to_markdown:31` 与 `clean_to_blocks:109`）
- [x] import 路径 `comet_rag.infrastructure.embedding.*` → `comet_rag.infrastructure.models.embedding.*`
- [x] `PipelineConfig` 配置项与 `engines/pipelines/types.py` 实际字段对齐（含 `docx` 子配置）

**验证：**
- [x] 文档中每段示例代码手动跑一遍，全部可执行
- [x] 理想情况：用 pytest 收集文档代码块自动执行

**依赖：** 无（可插队先做）
**文件：** `docs/pipeline_usage.md`
**规模：** XS

---

## Phase 1：任务框架落地

### ✅ T4 — `demo.py` 场景转 pytest

**描述：** `poc/task_demo/demo.py` 的 5 个场景已是事实上的测试，只是用 `print` 而非 `assert`。先固化行为，再搬代码——否则 T5 的迁移漂移无人察觉。

**验收标准：**
- [x] 5 个场景全部转为断言式测试：多阶段推进、协作式取消、可重试失败退避重排队、乐观锁与序列化往返、租约过期回收
- [x] 场景 1（含确认门）改写为**不含确认门**的多阶段推进 + 断点续跑（为 T5 的 A10 铺路）
- [x] 测试直接 import `poc.task_demo.task`，T5 迁移后只改 import 路径

**验证：**
- [x] `uv run pytest tests/unit/tasks -q` 全绿
- [x] 故意破坏 `states.py` 的一条迁移规则，测试应失败（验证测试真的在测东西）

**依赖：** T1
**文件：** `tests/unit/tasks/test_task_lifecycle.py`、`tests/unit/tasks/conftest.py`
**规模：** M

**产出：** 26 个用例（25 passed + 1 xfail），连跑 5 次零 flaky。
**关键发现：** 断点续跑当前只由确认门驱动，见 spec A10-修正，已扩大 T5 范围。

---

### ✅ T5 — `tasks/` 提升与去确认门

**描述：** 把 `poc/task_demo/task/` 搬到 `comet_rag/tasks/`，同时执行 A10（移除确认门）与 `Pipeline` 改名。

**验收标准：**
- [x] 6 个模块迁入 `comet_rag/tasks/`：`models / states / store / executor / runner / service`
- [x] `runner.Pipeline` 更名 `StagePipeline`（避开与 `engines/pipelines/pipeline.py::Pipeline` 撞名）
- [x] 移除：`AWAITING_REVIEW` 状态、`review_required/review_payload/review_decision/review_comment/reviewed_at` 五字段、`ReviewDecision`、`NeedsReview`、`TaskService.review()`、状态机中相关迁移
- [x] **保留**：`resume_stage`、`context`、`StagePipeline` 的阶段推进与续跑（A10 的关键约束）
- [x] 🔴 **新增（A10-修正）**：断点续跑改为**由失败驱动**。`executor._mark_failed`
      走可重试分支时须把当前 `stage` 写入 `resume_stage`，否则删掉确认门后
      `resume_stage` 永远为 None、变成死代码，重试退化为全量重跑
- [x] `comet_rag/schemas/task.py` 删除，全仓无残留引用

**验证：**
- [x] T4 的测试改完 import 后**一字不改**地全绿
- [x] `test_retry_should_resume_from_failed_stage` 的 `xfail(strict=True)` 标记
      可以删除并通过；同时 `test_retry_currently_restarts_pipeline_from_first_stage`
      需相应更新（它记录的是将被取代的旧行为）
- [x] `grep -rn "review\|AWAITING" comet_rag/tasks/` 无输出
- [x] `grep -rn "schemas.task\|schemas import task" comet_rag/` 无输出

**依赖：** T4
**文件：** `comet_rag/tasks/*.py`（6 个）、删除 `comet_rag/schemas/task.py`
**规模：** M（因 A10-修正而接近上限，若实现续跑时发现需改动 `StagePipeline`
契约，应拆出独立任务）

**产出：** 29 用例全绿，连跑 5 次零 flaky。断点续跑已由失败驱动。
**顺带修掉一个留痕 bug：** 退回 PENDING 时若不先把当前阶段收成 failed，
续跑时 `enter_stage` 会把那条失败记录关成 `succeeded`，阶段历史会骗人。
**新增 `TaskService.retry(from_scratch=True)`：** 怀疑前置阶段产出有问题时强制整条重来。
**未删除 `poc/task_demo/`**：该目录被 `.gitignore` 忽略、从未进过版本库，删掉不可恢复，留给你处置。

---

### ✅ T6 — 状态机全矩阵测试

**描述：** `states.py` 是纯函数零依赖，且 bug 后果最严重（"已取消的任务又变成成功了"，只在生产偶发）。投入产出比最高的测试。

**验收标准：**
- [x] 参数化覆盖去掉确认门后的 6×6 迁移矩阵，合法与非法各自断言
- [x] `can_transition(x, x)` 恒真（自迁移）
- [x] 终态（SUCCEEDED / CANCELLED）不可再迁出；FAILED 仅可迁往 PENDING
- [x] `states.py` 分支覆盖率 100%

**验证：**
- [x] `uv run pytest tests/unit/tasks/test_states.py --cov=comet_rag.tasks.states --cov-report=term-missing`

**依赖：** T5
**文件：** `tests/unit/tasks/test_states.py`（74 用例）、
`tests/unit/tasks/test_runner_and_service.py`（21 用例，为达 Checkpoint B 覆盖率补）
**规模：** S

**关键做法：** 期望矩阵**手写**，刻意不从 `_ALLOWED` 推导。
写成 `can_transition(a,b) == (b in _ALLOWED[a])` 是拿实现验证实现，恒真、一无所证。
手写表是独立的第二份真相，改状态机必须同时改它——那正是我们想要的摩擦。
**反向验证：** 放开终态出口、收紧 RUNNING→PENDING、新增状态值忘改表，三种
退化各自被精确捕获（用例 ID 直接点名坏掉的格子）。

---

### ✅ T7 — `TaskStore` 契约测试套件

**描述：** 写一套**与实现无关**的契约测试，`InMemoryTaskStore` 先过，T20 的 `PostgresTaskStore` 必须过同一套。这是"换存储时行为不变"这句承诺的唯一兑现手段。

**验收标准：**
- [x] 契约测试以 fixture 参数化接收任意 `TaskStore` 实现
- [x] 覆盖：CRUD、`idempotency_key` 幂等、乐观锁 CAS 冲突、`_IMMUTABLE` 字段拒改、`transition` 走状态机守卫、事件流递增、阶段留痕
- [x] 覆盖 `heartbeat(bump=False)` **不涨版本**（否则乐观锁退化为不停重试）
- [x] 覆盖 `sweep_stale`：还有重试次数退回 PENDING，否则判 FAILED
- [x] `InMemoryTaskStore` 全绿，覆盖率 ≥ 90%

**验证：**
- [x] `uv run pytest tests/unit/tasks/test_store_contract.py -q`
- [x] 并发场景：两协程同时 CAS 写同一任务，恰好一个成功一个抛 `VersionConflict`

**依赖：** T5
**文件：** `tests/contracts/task_store.py`（契约基类，37 用例）、
`tests/unit/tasks/test_store_contract.py`（InMemory 实现方）
**规模：** M

**产出：** 37 用例，`store.py` 覆盖率 99%。反向验证：把 heartbeat 改成涨版本、
把 sweep_stale 改成一律判死，各自恰好一个对应用例变红。

---

### ✅ T8 — `TaskExecutor` 契约测试套件

**描述：** 同 T7，但针对调度侧。`InProcessExecutor` 先过，T22 的 `ArqExecutor` 必须过同一套。

**验收标准：**
- [x] 覆盖：正常完成、重复 `submit` 幂等不跑两遍、协作式取消（`request_cancel` 返回 True ≠ 已停）、可重试失败退避重排队、`max_attempts` 耗尽判死、`shutdown` 优雅关停
- [x] 断言并发上限被强制执行：`max_concurrency=4` 时并发峰值 ≤ 4
- [x] 断言未注册 `kind` 走失败分支而非崩溃

**验证：**
- [x] `uv run pytest tests/unit/tasks/test_executor_contract.py -q`
- [x] 无 flaky：连跑 20 次全绿（`--count=20`，异步取消测试最易 flaky）

**依赖：** T5
**文件：** `tests/contracts/task_executor.py`（契约基类，15 用例）、
`tests/contracts/support.py`（只经 store 观察的等待原语）、
`tests/unit/tasks/test_executor_contract.py`（InProcess 实现方）
**规模：** M

**产出：** 15 用例，连跑 8 次零 flaky。反向验证：把信号量放大 100 倍，
并发闸门用例立刻变红。
**关键约束：** 契约只通过 TaskStore 观察结果，绝不 gather 执行器内部协程 ——
否则 ArqExecutor（跨进程，无本地协程）跑不了同一套。

---

## Phase 2：引擎修复

### ✅ T9 — `astream_run` 批量化（S4-3）

**描述：** `pipelines/pipeline.py:89` 逐 chunk `await aembed()`，200 chunk 的文档就是 200 次 HTTP 往返，GPU 大部分时间在等网络。同文件 `_aembed_chunks:148` 用的是 `abatch_embed`——同一个类两种写法。

**验收标准：**
- [x] `astream_run` 与 `stream_run` 改为按批 embed 后再 yield（批大小取自 `PipelineConfig`）
- [x] 流式语义保留：不得退化成"全部算完再一次性 yield"
- [x] 同步与异步两条路径行为一致

**验证：**
- [x] fake 模型记录调用次数：200 chunk、batch_size=32 时调用数 ≤ 7（而非 200）
- [x] 断言首个 chunk 的 yield 发生在全部 chunk 处理完成之前

**依赖：** T1
**文件：** `comet_rag/engines/pipelines/pipeline.py`、`types.py`、`tests/unit/engines/test_pipeline_embed.py`
**规模：** S

**⚠️ 验收标准已修正**：原写"调用数 ≤ 7"，前提是存在请求级批量。
实测不成立 —— `abatch_embed` 是扇出 N 个单条请求 + 信号量限流，
`Qwen3VLEmbeddingModel.embed()` 天生单条。改为验证**并发峰值**：
修复前恒为 1（完全串行），修复后 1 < peak ≤ max_concurrency。
详见 spec §8 S4-3 的修正记录。

---

### ✅ T10 — `url_loader` 复用 httpx client（S4-4）

**描述：** `loaders/url_loader.py:139,244` 每次加载都 `async with httpx.AsyncClient(...)`，重建连接池与 TLS 握手。对照 `infrastructure/models/embedding/qwen3_vl_embedding.py:112` 的注入式写法——那是本项目应统一采用的模式。

**验收标准：**
- [x] `UrlLoader` 构造函数接受 `async_client: AsyncClient | None`，缺省时自建并由自身生命周期管理
- [x] 同步路径同样处理
- [x] `cleanup()` / `__aexit__` 只关闭自建的 client，**不关闭外部注入的**

**验证：**
- [x] `grep -rn "AsyncClient(" comet_rag/` 结果全部为注入式或生命周期管理代码
- [x] 测试：注入一个 client 连续加载 3 个 URL，断言其未被关闭且仅创建一次连接

**依赖：** T1
**文件：** `comet_rag/engines/loaders/url_loader.py`、`tests/unit/engines/test_url_loader.py`
**规模：** S

---

### ✅ T11 — `PipelineHooks` 测试隔离（P8）

**描述：** `PipelineHooks` 用类变量做全局注册表。一旦开始写测试，A 测试注册的 hook 会泄漏到 B 测试。现在不暴露只是因为没有测试。

**验收标准：**
- [x] 提供 `PipelineHooks.snapshot()` / `restore()`，或改注册表为实例级并保留全局默认实例
- [x] `conftest.py` 提供 autouse fixture，每个测试后自动还原注册表
- [x] 公开 API 向后兼容，`docs/pipeline_usage.md` 的注册写法不变

**验证：**
- [x] 两个测试各注册同名 `extractor("txt")`，互不影响
- [x] 随机顺序跑测试仍全绿（`pytest -p no:randomly` 与随机序各跑一次）

**依赖：** T1
**文件：** `comet_rag/engines/pipelines/hooks.py`、`tests/conftest.py`
**规模：** S

---

### ✅ T12 — chunkers 单元测试

**描述：** 纯函数，输入输出明确，边界条件多。零外部依赖，最容易补齐覆盖率。

**验收标准：**
- [x] 覆盖全部 8 个 chunker（Text/Docx/Mdx/Python/TypeScript/Csv/Json/Xml）
- [x] 边界：空文本、纯空白、无分隔符超长文本、`chunk_overlap ≥ chunk_size`、单字符文本
- [x] 不变式：所有 chunk 拼接后覆盖原文（去除 overlap 后）；无空 chunk；`len(chunk) ≤ chunk_size` 或有明确例外并注明
- [x] `engines/chunkers/` 覆盖率 ≥ 80%

**验证：**
- [x] `uv run pytest tests/unit/engines/test_chunkers.py --cov=comet_rag.engines.chunkers`

**依赖：** T1
**文件：** `tests/unit/engines/test_chunkers.py`（不变式，8 类参数化）、
`tests/unit/engines/test_chunkers_variants.py`（其余 9 个代码分块器、CJK、
keep_separator=False、Mdx 首行标题、已知局限特征化）
**规模：** S

**覆盖率 96%**（目标 ≥80%）。code_chunker 与 text_chunker 均 100%。

**过程中的教训**：最初的测试文本是无空格无换行的中文，导致所有"不变式"
测试都绕过了分隔符递归逻辑、落到按字符兜底的退化分支。测试全绿，
但测的不是真正会跑的那条路 —— 是覆盖率报告（base_chunker 70%，
`_split_text_with_separator` 整段未执行）暴露的。换成带段落/句子/空格
结构的文本后，该文件覆盖率 70% → 94%。

**发现的已知局限**：`chunk_overlap` 按整个 split 保留而非按字符切，
当文本的自然切分单元大于 chunk_overlap 时**静默失效**（无报错）。
对 RAG 的影响：正是最需要重叠防止语义断裂的场景反而没有保护。
已写成特征化测试钉住当前行为，修复需改 `_merge_splits` 收缩策略（M1 之外）。

---

### ✅ T13 — `docx_parser` 快照测试

**描述：** 962 行零测试，是全仓改动风险最高的文件。**本任务只加测试，不重构**——先建保护网。

**验收标准：**
- [x] `tests/fixtures/docx/` 放入代表性样本：含标题层级、表格、图片、公式（OMML）、页眉页脚、嵌套列表
- [x] 每个样本一份快照，`DocxParser().parse()` 输出与快照比对
- [x] `omml.py` 公式转 LaTeX 单独测（616 行，逻辑独立）
- [x] 快照更新流程写入 `tests/README.md`，避免日后无脑覆盖快照

**验证：**
- [x] `uv run pytest tests/unit/engines/test_docx_parser.py -q`
- [x] 故意改动 `docx_parser.py` 一处逻辑，快照测试应失败

**依赖：** T1
**文件：** `tests/unit/engines/test_docx_parser.py`、`tests/fixtures/docx/*`、`tests/README.md`
**规模：** M

---


**产出**：`docx_parser.py` 覆盖率 82%、`omml.py` 92%。
**样本为生成而非提交二进制**：.docx 是 zip，进 git 即不可 review；
且 `poc/docs/` 里的真实文档含实际项目内容，本项目计划开源不应提交。
补偿措施：另有一条可选用例，本地存在 poc/docs 时拿 4 个真实 Word 文档跑冒烟（已通过）。
**发现疑似缺陷**：`<m:d>` 无 dPr 时 `do_d`(omml.py:360) 返回裸内容，括号丢失；
而 D_DEFAULT 已定义默认圆括号、同模块 do_f 在 fPr 缺失时会套默认值。
已标 xfail(strict) + 当前行为特征化测试，M1 内不重构（plan R2）。

## Phase 3：第一条端到端切片（全内存）★

### ✅ T14 — `BaseVectorStore` 接口重设计 + `InMemoryVectorStore`

**描述：** 现有 ABC 缺 `kb_id` / 分区参数。本任务定死接口并配契约测试——T21 的 `MilvusStore` 必须过同一套。内存实现不是玩具，它是验证抽象没绑死 Milvus 的工具。

**验收标准：**
- [x] 接口含 `kb_id` 维度；`filter` 参数是结构化 `dict`，**禁止**接收 Milvus 表达式字符串
- [x] 新增 `aensure_collection(kb_id, dim, ...)`，隐藏"Milvus 要显式建表、内存版不用"的差异
- [x] `InMemoryVectorStore` 实现（numpy 余弦相似度，约 80 行）
- [x] 契约测试含 **"写入后立即查询"** 用例（拦截 Milvus flush 语义差异，R1）
- [x] 契约测试含"维度不符应报错"用例（A12）

**验证：**
- [x] `uv run pytest tests/unit/infrastructure/test_vectorstore_contract.py -q`
- [x] 接口签名中不出现任何 Milvus 专有概念（人工 review）

**依赖：** T1
**文件：** `comet_rag/infrastructure/vectorstore/__init__.py`、`base.py`、`memory.py`、`tests/unit/infrastructure/test_vectorstore_contract.py`
**规模：** M

---


**产出**：27 个契约用例。反向验证用一个模拟 Milvus「写入后不 flush 查不到」
的假实现跑契约，**14 个用例失败** —— T21 忘了 flush/load 会当场变红（plan R1）。
**刻意不用 numpy**：它目前只是 pdftext（MinerU 依赖）带进来的传递依赖、未声明，
用了就会让不装 mineru extra 的用户崩 —— 与 T2 修掉的 pyyaml 漏声明同类。纯 Python 足够。

### ✅ T15 — `services/ingestion.py` 入库 runner

**描述：** 把 `engines.Pipeline` 与 `tasks` 框架接起来：注册 `kind="ingest"` 的多阶段 runner。这是"生产端投递、消费端执行"落地的第一块。

**验收标准：**
- [x] 用 `StagePipeline` 定义阶段：`loading → parsing → chunking → embedding → upserting`
- [x] 每阶段调 `ctx.checkpoint()`（协作式取消）与 `ctx.report(progress=...)`
- [x] 中间态写 `ctx.put()` 且**可 JSON 序列化**（断点续跑前提）
- [x] 模型/向量库调用超时与网络错误包成 `RetriableError`；解析失败等确定性错误**不可重试**
- [x] chunk metadata 带 `kb_id`

**验证：**
- [x] 用 fake 模型 + `InMemoryVectorStore` + `InMemoryTaskStore` 跑通全部阶段
- [x] 在 `embedding` 阶段注入失败，重试后**从 embedding 而非 loading 开始**（断点续跑，A10 关键约束）

**依赖：** T5、T9、T14
**文件：** `comet_rag/services/ingestion.py`、`tests/unit/services/test_ingestion.py`
**规模：** M

---

### ✅ T16 — `services/retrieval.py` 检索

**描述：** query → 向量召回 → rerank → 排序结果。

**验收标准：**
- [x] 按 `kb_id` 限定检索范围
- [x] rerank 可选（无 reranker 时跳过，不报错）
- [x] 返回结构含 `score`、`text`、`metadata`，按分数降序
- [x] `top_k` 与 rerank 前的召回数（`fetch_k`）分开配置

**验证：**
- [x] 灌入已知内容后检索，断言目标 chunk 排在首位
- [x] 跨 `kb_id` 检索**不得**串出别的知识库内容

**依赖：** T14
**文件：** `comet_rag/services/retrieval.py`、`tests/unit/services/test_retrieval.py`
**规模：** M

---


**阶段划分与原计划不同（3 而非 5），两处合并都有硬理由**：
· 取源合进 extracting —— 它的产出是本地临时文件路径，不是跨进程可恢复的状态，
  换个 worker 续跑时文件根本不存在；重试时重新下载才是正确行为。
· 向量化与写入合成 indexing —— 拆开的话向量要经 context 跨阶段，
  200 chunk × 1024 维会撑爆任务表。合并后按窗口边算边写、内存有界，
  且 upsert 按稳定 chunk id 幂等，整段重跑安全。
**产出**：39 用例，services 覆盖率 99%。反向验证：关掉断点续跑后
`test_retry_resumes_from_indexing_without_reparsing` 立刻变红。

### ✅ T17a — `Context` 与 `lifespan` 资源装配

**描述：** `api/lifespan.py` 的资源初始化目前全是注释，`core/context.py` 是 0 字节空文件。本任务只做资源装配，不碰路由。

**验收标准：**
- [x] `core/context.py` 定义 `Context`，持有 store / executor / vectorstore / 模型 / httpx client
- [x] `lifespan` 按配置装配并挂 `app.state.ctx`；关停时**逆序**释放
- [x] 后端实现由配置决定（内存 / 真实），为 Phase 4 的逐个替换留好开关
- [x] `api/deps.py` 从 `app.state.ctx` 取依赖，路由不得直接 new 资源

**验证：**
- [x] 起停应用无资源泄漏警告；httpx client 全局仅一个实例
- [x] 配置切换到内存后端时，启动不触碰任何中间件

**依赖：** T15、T16
**文件：** `comet_rag/core/context.py`、`api/lifespan.py`、`api/deps.py`
**规模：** S

---

### ✅ T17b — API 路由接线

**描述：** `/search` 目前直接回显 query（`api/routes/search.py:8`）。本任务把路由接到 T17a 装配好的服务上。

**验收标准：**
- [x] 路由：`POST /ingest`、`GET /tasks/{id}`、`GET /tasks`、`POST /search`、KB 的 CRUD
- [x] `/tasks/{id}` 返回 `Task.public_view()`（**不泄漏** traceback、worker_id、context）
- [x] 出入参 Pydantic 模型补齐（`schemas/` 下 `ingest.py`、`task.py`、`kb.py`；注意 `schemas/task.py` 已被 T5 删除，此处是全新的 API 层模型，不是任务领域模型）
- [x] 异常映射：`TaskNotFound` → 404、`VersionConflict` → 409、闸门超限 → 429

**验证：**
- [x] `uv run pytest tests/e2e/test_ingest_search.py -q`（内存后端，**无需 docker**）
- [x] `uv run uvicorn comet_rag.api.main:app` 能起，`/docs` 可访问

**依赖：** T17a
**文件：** `api/routes/ingest.py`、`routes/tasks.py`、`routes/search.py`、`routes/kb.py`、`schemas/*.py`
**规模：** M

---

> ## ★ Checkpoint C —— 全计划最重要的评审点
> 端到端在**零中间件**下跑通。此时若发现抽象设计有问题，修复只需改代码；
> 一旦进入 Phase 4 之后再发现，就要动数据。**务必人工评审后再继续。**

---


**装配路径新增 `core/bootstrap.py`（组合根）**：唯一知道"用哪个实现"的地方。
后端由 `backends` 配置段决定（memory / milvus / postgres / inprocess / arq），
业务代码里不出现任何 `if backend == ...`。worker 进程将复用同一套装配。

**`main.py` 改成 `create_app()` 工厂 + PEP 562 懒加载 `app`**：
原先模块级 `get_config()` 让"import 这个模块"等价于"必须有一份合法配置"，
测试与工具全被绑架；且 e2e 得以走**真实装配路径**而非另抄一份接线。

**发现并修掉两个既有缺陷**（详见提交说明）：
· `schemas/__init__.py` 自 T5 起 import 已删除的 `task.py` —— 全仓 600+ 测试全绿却没人发现
· `setup_logging()` 把 patcher 装在 `logger.patch()` 的副本上而非全局，
  导致所有 `from loguru import logger` 的模块日志被**静默丢弃**

## Phase 4：换上真实后端

### ✅ T18 — docker-compose + database + alembic

**描述：** 首次引入外部中间件。只做接线，不含业务表。

**验收标准：**
- [x] `docker-compose.yml`：postgres、redis、milvus（含 etcd/minio）、minio
- [x] `infrastructure/database/`：async engine、session factory、`Base`
- [x] alembic 初始化并配好 async 模板
- [x] `config/schemas.py` 按 A6 删除 `MongoConfig`；补 `RedisConfig` 等的实际接线

**验证：**
- [x] `docker compose up -d && uv run alembic upgrade head` 成功
- [x] `uv run pytest -m integration` 能连上各中间件

**依赖：** T2
**文件：** `docker-compose.yml`、`comet_rag/infrastructure/database/*.py`、`alembic/`、`config/schemas.py`
**规模：** M

---


**产出**：5 个容器全部健康（postgres / redis / milvus / etcd / minio），
7 条集成冒烟用例通过。

**端口避让**：MinIO 映射到 9010/9011 而非 9000/9001 —— 本机 9000/9001 已被占用，
撞了之后的报错很难一眼看出原因。

**alembic.ini 的 DSN 刻意留空**：该文件进版本库，写死密码迟早误提交。
取值逻辑在 `env.py`：优先 `-x dsn=...`，否则读 config.yaml。

**中间件没起时集成用例 skip 而非 fail**，已反向验证（停掉 redis 后
对应用例 skip、其余照跑）。让核心依赖环境红一片只会训练出"看到红色就忽略"。

### ✅ T19 — `knowledge_bases` 表 + KB service（A12）

**描述：** KB 必须是表而非纯字符串标签——**要记录 embedding 模型版本**。否则换模型后新旧向量混在同一空间，检索**静默劣化**且事后无法分辨该重算哪些 chunk。

**验收标准：**
- [x] 表含：`kb_id`、`name`、`embedding_model`、`embedding_dim`、`created_at`、`description`
- [x] `services/knowledge_base.py`：create / get / list / delete
- [x] 建 KB 时按 `embedding_dim` 调 `aensure_collection`
- [x] 向已有 KB 写入维度不符的向量**必须报错**，不得静默写入
- [x] 删除 KB 同时清理对应 Milvus partition

**验证：**
- [x] `uv run pytest -m integration tests/integration/test_kb.py`
- [x] 维度不符用例断言抛出明确异常

**依赖：** T18、T14
**文件：** `comet_rag/infrastructure/database/models.py`、`services/knowledge_base.py`、alembic 迁移、`tests/integration/test_kb.py`
**规模：** M

---


**知识库元数据也做了 ABC + 两实现 + 契约**（内存 / Postgres，10 条契约两边都过）。
不这么做的话 Checkpoint C 的"e2e 零 docker 可跑"当场就没了 —— 而 Checkpoint D
要求那条 e2e 在真实后端下一字不改地继续通过。

**A12 有两个执行点**：
· 建库/重复建库 → `KnowledgeBaseService.create` 校验模型一致
· 入库前 → `IngestRunner` 调 `resolve_for_ingest` 校验，且**维度取自知识库**
  而非配置（同一进程可服务多个不同维度的库）
维度不符能被向量库拦下，但**同维度的不同模型谁也拦不住** —— 只有这张表能。

**删除顺序**：先删向量、后删元数据。反过来中途失败会留下无主向量 ——
没有元数据就没人知道它们属于谁、该不该清，只能人工翻库。

**首个 alembic 迁移已验证双向可用**（upgrade → downgrade → upgrade），
且命名约定生效（`pk_knowledge_bases` 而非数据库随机命名）。

### ✅ T20 — `PostgresTaskStore`

**描述：** 实现 `TaskStore` 的 7 个抽象方法。业务规则（状态机守卫、时间戳、事件留痕）由基类模板方法提供，**不得重写**。

**验收标准：**
- [x] `tasks` 与 `task_events` 两张表；`status` 列为 **varchar 而非 PG enum**（保留将来加状态值的零成本可逆性）
- [x] `_save` 用 `UPDATE ... WHERE version = :expected` 实现 CAS
- [x] `heartbeat` 走 `bump=False` 路径，不涨版本
- [x] `idempotency_key` 建唯一索引
- [x] 通过 **T7 的同一套契约测试**，一行不改

**验证：**
- [x] `uv run pytest -m integration tests/integration/test_store_postgres.py`
- [x] 并发 CAS：10 协程同时写，恰好 1 成功 9 冲突

**依赖：** T18、T7
**文件：** `comet_rag/tasks/store_postgres.py`、alembic 迁移、`tests/integration/test_store_postgres.py`
**规模：** M

---


**产出**：40 条用例通过（T7 的 37 条契约 + 3 条 Postgres 特有），
另有 4 条端到端跑在 Postgres 后端上、断言与内存版逐字相同。

**两处并发用了两种手段，因为冲突概率的量级不同**：
· 乐观锁（任务状态）—— 同一任务同时只有一个 runner 推进，冲突罕见
· 悲观锁（事件序号）—— 所有写入者抢同一个号，冲突**必然**

事件序号最初也想用乐观思路（子查询取号 + 冲突重试），12 路并发下直接崩了。
重试次数加多少都只是把失败概率往后推。改用 `SELECT ... FOR UPDATE` 锁父任务行。

**⚠️ 过程中发现自己写了一条空转的测试**：最初的"真并发 CAS"用例，
在把原子 CAS 换成"先查后写"之后**照样全绿** —— asyncio 的调度让每个事务
读→写→提交一气呵成，危险窗口根本没出现。加 `asyncio.Barrier` 强制
"所有读先于任一写"后才真正区分开：原子 CAS 出 1 个赢家，先查后写出 10 个
（9 次更新被静默丢弃）。**反向验证不是形式，它这次抓到的是测试本身的缺陷。**

### ✅ T21 — `MilvusStore`

**描述：** 全计划风险最高的实现（R1）。Milvus 写入后不 flush 查不到，这类差异不体现在签名上，只能靠契约测试拦截。

**验收标准：**
- [x] collection schema 含 `id`、`kb_id`（partition key）、`text`、`dense_vector`、**预留的 `sparse_vector` 字段**（A11，M1 不写入）、`metadata` JSON
- [x] 按 `kb_id` 分区
- [x] `filter` dict 转 Milvus 表达式的逻辑**封装在实现内部**，不外泄
- [x] 写入后正确处理 flush/load，保证"写完立即可查"
- [x] 通过 **T14 的同一套契约测试**，一行不改

**验证：**
- [x] `uv run pytest -m integration tests/integration/test_vectorstore_milvus.py`
- [x] 契约测试的"写入后立即查询"用例必须过（R1 的拦截点）

**依赖：** T18、T14
**文件：** `comet_rag/infrastructure/vectorstore/milvus.py`、`tests/integration/test_vectorstore_milvus.py`
**规模：** M

---


**产出**：T14 的 27 条契约一行未改地通过，另加 4 条全栈端到端
（Postgres + Milvus 同时真实）。

**⚠️ 与本条目原文的偏离**：原写"kb_id 作 partition key"，那基于"多 kb 共用
一个 collection"的假设。实际按已确认的**一 kb 一 collection**实现，
partition key 不需要；kb_id 仍写进 metadata，为将来可能的迁移留路。

**三个关键决定都是实测出来的，不是猜的**：
· 一致性级别 `Session` 而非每次 flush —— 实测 Bounded（默认）写完立即查
  **命中 0 条**；Session 0.34s 可见、Strong 0.62s。每次 flush 会封存 segment、
  把吞吐打垮，是错的解法。
· sparse 字段可预留 —— 实测空 dict `{}` 可插入、省略字段则报 DataNotMatch；
  且 Milvus 要求所有向量字段 load 前都有索引，故字段与索引一起建。
· kb_id → collection 名需映射 —— Milvus 只接受 `[A-Za-z_][A-Za-z0-9_]*`，
  而 kb_id 可含中文与连字符。用"可读部分 + 短哈希"，避免 `a-b` 与 `a_b` 撞名。

**反向验证**：把一致性退回 Bounded，5 条用例变红。值得注意的是
`test_upsert_is_visible_immediately` 那次**恰好没红** —— Bounded 有陈旧容忍
窗口，数据有时来得及传播。这正说明该类缺陷为何危险：开发机上"能跑"，
上了负载才偶发。

## Phase 5：跨进程与容错

### ✅ T22 — `ArqExecutor`

**描述：** 实现 `TaskExecutor` 的 ARQ 版。**队列里只放 `task_id`**，任务数据一律从 `TaskStore` 读——这是重试、崩溃恢复、断点续跑能退化成同一个动作 `submit(task_id)` 的前提。

**验收标准：**
- [x] `submit()` → `enqueue_job("run_task", task_id)`；~~`_job_id` 用 `task_id`~~ 用 `task_id:attempts`（见下方偏离说明）
- [x] 跨任务复用 redis 连接（A3 选 ARQ 的核心理由，必须兑现）
- [ ] 跨任务复用 httpx 连接池 —— 由 worker 侧共享 `Context` 提供，**归 T23**
- [x] `request_cancel` 跨进程语义：写 CANCELLING 状态，由 worker 的 `ctx.checkpoint()` 感知
- [x] 通过 **T8 的同一套契约测试**（15 条，一行未改）

**验证：**
- [x] `uv run pytest -m integration tests/integration/test_executor_arq.py` → 21 passed
- [x] 断言入队 payload 只含 `task_id`，不含任务数据

**依赖：** T8、T18
**文件：** `comet_rag/tasks/executor_arq.py`、`tests/integration/test_executor_arq.py`
**规模：** M

**产出**：T8 的 15 条执行器契约**一行未改**地通过。契约当初就是照这一天设计的
（只经 TaskStore 观察、绝不 gather 内部协程），这次是兑现。另加 6 条 ARQ 专项。

**⚠️ 与本条目原文的偏离：`_job_id` 不能只用 `task_id`。** 实测：arq 的
`enqueue_job` 在 `arq:job:{id}` 或 `arq:result:{id}` 存在时直接返回 `None`，
而结果键默认保留**一小时**。只用 task_id 的话，第一次失败后的重投会被自己
上一轮的结果键挡掉，任务永远停在 PENDING，**且不报任何错**。改用
`task_id:attempts`：同一次尝试内重复投递照样去重，跨尝试的重投是新 job。
`test_job_id_must_vary_per_attempt` 把退化写法的现场固定了下来。

**先做了一次重构**：把 `InProcessExecutor` 里与调度无关的部分提到
`StoreDrivenExecutor`（RUNNING 抢占、超时、失败分类、续跑锚点、取消受理）。
两种执行器真正的差别只剩三处：拉起方式、重试怎么重排队、有没有本地
hard cancel。不这么做就要抄一份一百来行的状态推进逻辑，而它一定会漂 ——
"漂"的症状正是"换个部署方式行为就变了"，那是本项目最想消灭的东西。
重构后 176 条既有用例全绿，才动手写 ArqExecutor。

**跨进程逼出了一个真 bug（乐观锁没有重读重试）**：runner 一边 `enter_stage`，
另一边有人请求取消，两次写入撞在同一个版本号上 —— `transition` 直接抛
`VersionConflict`，取消**失败了**，任务永远停不下来。
根因是 `TaskStore` 把两种语义混为一谈：调用方**没传** `expected_version` 时，
CAS 只是方法内部"读—改—写"的护栏，撞了就该重读重来；**传了**才是
"我要的就是这一版"的断言，那种冲突必须原样上抛。修在基类 `_cas()`，
所有实现一起受益，且守卫在每一轮重读后重新过一遍。
内存实现跑不出这个 bug（读写之间没有真正的 await 间隙），
换成 Postgres + 真 Redis 后窗口宽到必撞 —— 这正是 plan "先内存后真实" 里
"真实后端阶段还能抓到东西" 的那一类收获。
**反向验证**：`_CAS_RETRIES` 调回 1，跨进程取消用例 3/3 稳定变红。

**顺带修掉一处会变成 500 的竞态**：`TaskService.submit` 读到 PENDING 与真正
入队之间隔着一次网络往返，worker 可能已把任务捞走，`executor.submit` 于是
以"只有 PENDING 可提交"拒绝。但调用方的目的（任务排上了）其实已达成，
一次幂等的重复 POST 不该变成 500。只在**确认它真的离开了 PENDING** 时才咽下，
否则照抛（`test_submit_still_raises_when_the_task_really_is_pending` 守着这条）。

---

### ✅ T23 — workers 入口

**描述：** 按**负载特征**分 worker，不按业务名词分。preprocessor 是 CPU 密集（多进程扩容），embedder 是 IO 密集（单进程高并发）——**扩容方式反了会让模型服务过载排队，整体更慢**。

**验收标准：**
- [x] `workers/preprocessor.py`：解析+清洗+分块，CPU 密集部分走 `to_thread`（不用 `ProcessPoolExecutor`，理由见下）
- [x] `workers/embedder.py`：向量化+写入，单进程内用信号量控制并发（两级闸门）
- [x] 两者共享 `Context` 装配逻辑，不各自 new 资源
- [x] 两个 `WorkerSettings` 的并发参数默认值与注释说明扩容方式差异

**验证：**
- [x] `uv run arq comet_rag.workers.preprocessor.WorkerSettings` 能起并消费
- [x] e2e：API 提交 → worker 消费 → 状态推进至 SUCCEEDED

**依赖：** T22、T15
**文件：** `comet_rag/workers/preprocessor.py`、`workers/embedder.py`、`workers/base.py`
**规模：** M

**先要回答一个本条目没写的问题：分开的两个 worker，各自跑哪些阶段？**
入库是**一个** kind、一条三阶段流水线。两个 worker 若都消费同一条队列，
"CPU 密集 / IO 密集"就只是文档里的措辞——解析照样会落到 embedder 上，
把它的事件循环堵死。**分道必须能把阶段路由到不同的池**，否则等于没分。

**实现：给阶段声明 lane，换道时移交。**
`StagePipeline.stage(name, lane=...)`；`TaskContext.lane` 是本 worker 服务的道；
下一个阶段的 lane 与之不同时，流水线返回新的 `Handoff` 结果 —— 任务退回
PENDING、写 `resume_stage`、投到目标道的队列。

关键在于**没有引入新的执行模型**：移交复用的正是断点续跑那套
`resume_stage` 机制，两者在实现上是同一件事，区别只在原因。
于是单进程部署（`lane=None`）一行行为都没变 —— 762 条既有用例全绿。

切口选在 chunking 与 indexing 之间，因为那里同时是**负载性质翻转处**
（解析→调模型）和**中间态最小处**（只有一串 chunk 文本要交接；
若切在 indexing 中间，就得把 200×1024 维的向量塞进任务表）。

**移交不能算作一次尝试。** `execute()` 进 RUNNING 时把 attempts 加了 1，
移交时必须退回去。不退的话，一条两次移交的流水线开跑即吃掉 2 次重试
预算，真出故障时反而没得重试。`test_handoff_does_not_consume_a_retry_attempt`
守这条；反向验证：去掉那行退账，用例立刻变红。

**job_id 里还得再加 lane。** 移交不涨 attempts，只靠 `task_id:attempts`
的话，投给 io 道的新 job 会被 cpu 道刚写下的结果键挡掉 —— 又是 T22 那个
"静默卡死"。这是同一个坑的第二次出现，也说明它确实容易踩。

**为什么不用 `ProcessPoolExecutor`（与本条目原文的偏离）。**
本条目写的是"`to_thread` / `ProcessPoolExecutor`"。选了前者，因为：
extractor 是运行时注册进 `PipelineHooks` 的可调用对象，跨进程要求可 pickle；
而 preprocessor 本来就靠**副本数**扩容，再套一层进程池是把同一件事做两遍，
却多出一套要单独调参与监控的东西。`to_thread` 的作用不是并行（GIL 挡着），
而是让心跳与 `ctx.checkpoint()` 在解析期间还跑得动 —— 这一点已写进注释，
免得后人以为它能并行而去调大 `max_jobs`。

**并发默认值本身就是设计**：preprocessor 2 / embedder 32。
`tests/unit/test_worker_profiles.py` 把这个不对称钉住了 ——
两个值对调时立刻变红（已反向验证）。同一文件还拦住另一个失效模式：
**流水线声明了一条没有 worker 服务的 lane**，任务会静静停在没人消费的
队列上，PENDING、无错误、无日志，从 API 上完全看不出来。

**顺带兑现 T22 挂账的那条**：跨任务复用 httpx 连接池 —— 由 worker 侧共享
`Context` 提供，`test_workers_reuse_one_http_client_across_tasks` 守着。

**顺手修掉一个会让测试静默挂起的坑（不属于 T23，但被它撞出来了）**：
连跑两遍集成测试时，第二遍在第 8 个用例上挂死，11 分钟没有任何输出才被
人工掐掉 —— 卡在哪个用例都看不出来。根因是 `TRUNCATE` 要 ACCESS EXCLUSIVE
锁，而 PostgreSQL 默认**无限期等**。四个测试文件各自拼了一份 TRUNCATE，
现在统一走 `conftest.truncate_tables`，先 `SET LOCAL lock_timeout='5s'`，
拿不到锁就报错并指出"多半是上一个用例漏关了会话"。
静默挂起比失败糟得多：前者连从哪儿查起都不知道。

**另一处隔离修正**：`test_e2e_workers` 用的是**生产队列名**（它验的就是生产
`PROFILE`），而它开场要清队列 —— 用 db 0 的话会把开发机上真跑着的
`comet:cpu` / `comet:io` 一起删掉。改用独立的 redis db 15。

**记一条给 T28 的账**：`pyproject.toml` 没有 `[build-system]`，包没被装进
venv，因此 `arq comet_rag.workers.…` 只能在项目根目录下跑（config.yaml 也
是从 cwd 读的，两者恰好一致）。T28 要加 `comet-rag serve` 控制台脚本，
届时必须先补上构建后端，顺便解决"配置文件路径不可指定"。

---

### ✅ T24 — `sweep_stale` 与崩溃恢复

**描述：** `store.py` 注释写明"单进程不要挂这个定时器"。上 ARQ 后是跨进程，**必须挂**——否则 worker 崩了任务永远卡在 RUNNING。

**验收标准：**
- [x] ARQ cron job 定期跑 `sweep_stale(lease)`，`lease` 可配且 > 心跳间隔数倍（90s vs 10s，9 倍）
- [x] 单进程模式（`InProcessExecutor`）**不得**启用，避免一份任务两个执行者
- [x] 回收动作打日志并写 `TaskEvent`

**验证：**
- [x] 集成测试：任务跑到一半 `kill -9` worker，租约超时后任务退回 PENDING 并被另一 worker 接管完成
- [x] 断言单进程模式下定时器未注册

**依赖：** T23
**文件：** `comet_rag/workers/maintenance.py`、`tests/integration/test_crash_recovery.py`
**规模：** S

**"单进程不得启用"不是靠开关，是靠结构。** 开关会被配错，而且配错的症状
（一份任务两个执行者）没有任何报错。`sweep_cron` 只在 `workers/` 下注册，
单进程部署根本不加载那个包。`test_layering.py` 用 AST 把这条钉住：哪天有人
图省事在 `core/bootstrap.py` 里 import 它，立刻变红（已反向验证）。

**回收后必须重新入队，这一步极易漏。** `sweep_stale` 只改数据库状态，而 ARQ
部署下"PENDING"不代表队列里有它 —— 漏了这步，任务只是从"卡在 RUNNING"
变成"卡在 PENDING"，看着更健康，实际一样没人跑。反向验证：去掉重投，
崩溃恢复用例立刻红。

**投回哪条道也要挑。** 回收只知道 task_id，于是照 `resume_stage` 反查它属于
哪条道（`StagePipeline.lane_of`）。投错虽能自愈（目标 worker 会再移交一次），
但不能指望 —— 回收往往正是因为某条道整个挂了，把任务投进那条道等于让它接着躺着。

**必须补的一道围栏：`_still_mine`。** 租约判死做不到准确 —— "没心跳"和"死了"
本来就分不清。所以一定存在这种局面：原 worker 其实还活着、只是慢了，被回收后
它跑完照样想写终态。不拦的话：

  · 写成功 → 覆盖掉接管者正在做的那一份；
  · 写失败（PENDING → SUCCEEDED 非法）→ 异常被 `execute()` 收口成
    `_mark_failed`，**把一条正在被别人正常执行的任务判死**。

第二种尤其恶劣：任务明明成功了最终却是 FAILED，看日志还像是 runner 自己出的错。
所以是 **lease 负责少误判，围栏负责误判了也不出错**，两者缺一不可。
反向验证：拆掉围栏，`test_stale_worker_cannot_write_after_being_reclaimed` 变红。

**崩溃用真进程 + 真 SIGKILL。** 在测试进程里 cancel 一个协程不算崩溃 ——
`execute()` 会接住 `CancelledError` 把任务干净落成 CANCELLED，恰好绕开要验的
那条路。子进程跑的是 `arq` 命令行本身，所以这条链路顺带也验了 CLI 入口。

**定时器挂在每个 worker 上，不单起进程。** arq 的 cron 默认 `unique=True`，
job id 里带着计划时刻，多副本同时到点也只有一个能真正入队。若改成 False，
N 个副本会同时回收同一批任务 —— 那正是回收要防的"多个执行者"，只是搬到了
回收器自己身上。`test_workers_carry_the_sweep_cron_and_it_is_unique` 守这条。

---

## Phase 6：资源治理与收尾

### ✅ T25 — 并发闸门与有界背压（S4-1、S4-2）

**验收标准：**
- [x] 对模型服务的调用统一经过闸门，并发上限可配；**不允许任何地方裸调**
- [x] 所有队列有界，满时拒绝或阻塞，绝不无限堆积
- [x] 超限时返回明确错误（HTTP 429 / `RetriableError`），不静默丢弃

**验证：**
- [x] 配 `model_concurrency=4`，fake 模型记录并发峰值，断言 ≤ 4
- [x] 投递量 10× 于处理能力，进程存活且积压始终有界

**依赖：** T23
**文件：** `comet_rag/core/concurrency.py`、`infrastructure/models/*/base.py`、`services/*.py`
**规模：** S

**先实测出了一个真缺陷：配置说 4，实际 128。**
`abatch_embed` 原本每次调用都新建一个 `asyncio.Semaphore(max_concurrency)`，
那只约束**这一次调用内部**的扇出，任务之间毫无关系：

    embedder 的 max_jobs=32 × 每个任务 4 路扇出 = 对模型服务 128 路并发

而配置里写的是 4。更糟的是监控上完全看不出来 —— 每个任务都觉得自己很守规矩。
真正该被约束的是**进程对模型服务的总并发**，所以闸门必须是进程级、共用一个对象的。

**闸门放在基类而不是包装器上。** spec §7 写的是"不允许裸调"。包一层装饰器
也能限流，但那只是**约定**：谁直接拿着模型调一下就绕过去了，且不报任何错。
改成模板方法：`aembed()` / `ascore()` 是唯一对外入口且不可覆写（闸门在这里），
子类实现 `_aembed()` / `_ascore()`。于是绕过闸门在结构上做不到。
代价是所有实现与替身都要改方法名 —— 值得，这条验收标准从此有了牙齿。

**embedding 与 rerank 共用同一个 `Gate` 实例**：它们抢的是同一块 GPU，
各配一个等于没配（两边都配 4，服务端看到 8）。

**有界不只是"有个上限"，还要有拒绝的出口。** 闸门满了之后无限排队等于把
OOM 往后推 —— 等待者本身占内存，而且等五分钟才轮到的请求上游早超时了。
所以两道界：`model_queue`（等待席位）与 `model_wait_timeout`（最长等待），
越界抛 `Overloaded` → HTTP 429 / 可重试失败。

**积压也要有界**：`TaskService` 在**建记录之前**查 PENDING 数，到顶就抛
`Backlogged` → 429。建完再拒等于白建一条，还会污染积压统计。

**自己写的边界差了一条。** 判据最初写成 `> max_backlog`，实测积压峰值是 6
而上限是 5 —— 已经到 N 了再收一条就是 N+1，该用 `>=`。
是 e2e 用例把这一条抓出来的。

**反向验证补了一次课。** 头一轮只测了 `Gate` 本身与模型层，把 bootstrap 里
那行 `bind_gate` 删掉**全套测试照样全绿** —— 也就是说"不允许裸调"这条验收
标准当时实际上没有任何东西守着。补了 `test_bootstrap.py` 里两条盯着装配
动作本身的用例，再删就红了。

新增 `/admin/limits` 暴露闸门与积压的实时数字：限流是否生效不该靠猜。

---

### ✅ T26 — 分级降级（S4-5）

**验收标准：**
- [x] 降级顺序：先关 rerank → 再降 `top_k` → 最后拒绝新任务
- [x] 每次降级**必须打日志**（否则线上无法察觉质量下降）
- [x] 触发条件可配（模型服务超时率 / 队列深度）

**验证：**
- [x] 注入模型服务超时，检索仍返回结果（无 rerank）且日志有降级记录

**依赖：** T25、T16
**文件：** `comet_rag/core/degradation.py`、`services/retrieval.py`
**规模：** S

**顺序的道理：先砍最贵、最可有可无的。** rerank 是交叉编码器，每次查询要给
几十个候选打分，是读路径上最重的一步，而没有它检索**仍然可用**（向量召回
本身就是完整答案）。top_k 次之。拒绝写入放最后 —— 那是唯一让用户"什么也
得不到"的一级。顺序反了的话，资源紧张时会先砍掉写入，而读路径照样慢。

**两个判据缺一不可。** 只看失败率：模型服务健康但请求量暴涨时，队列越排越长
而失败率还没上来，等它上来早就雪崩了。只看队列深度：模型服务变慢（但没满）
时队列可能并不深，却已经在拖 p99。

**必须有滞回。** 阈值一刀切的话，指标在边界附近来回时 rerank 会被反复开关，
行为不可预测、日志刷屏。所以升级立刻生效、降级要过冷却期，且一次只恢复一级
（直接跳回正常容易立刻又被打回去）。
`test_flapping_metrics_do_not_flip_the_level_back_and_forth` 专盯这条。

**日志只在级别变化时打。** 每次调用都打的话，过载时日志本身就成了新的负担 ——
而过载正是最需要日志还能用的时候。不打则线上质量悄悄下滑无人察觉，
S4-5 明确要求了留痕，两头都得顾。

**观测点挂在闸门上。** 所有对模型服务的调用都必然穿过 `Gate`，所以让它顺手把
成败报上来，不必在每个调用点插桩 —— 与"闸门本身绕不过去"是同一个理由，
也就不会有谁忘了插。取消不计入失败率：那是调用方主动放弃，把它算进去会让
"用户取消了几个任务"看起来像服务故障。

**L3 放在 API 层。** `comet_rag/tasks/` 是与产品无关的通用框架，不该认识"降级"；
而"收不收这个请求"本来就是入口的职责。读路径不受影响，正是这一级要保住的东西。

**反向验证**：拿掉滞回、对调降级顺序、闸门不报失败、检索不问控制器 ——
四种退化各自被对应的用例抓住。

---

### ✅ T27 — benchmark 基线（S4-6）

**验收标准：**
- [x] `tests/benchmark/` 覆盖：单文档入库耗时、批量入库吞吐、检索 P50/P95/P99
- [x] 输出可存档（`bench-report.json`），供 PR 前后对比（`--bench-baseline`）
- [x] 基线数值记入 `docs/benchmark.md`

**验证：**
- [x] ~~`--benchmark-only`~~ → `uv run pytest -m benchmark` 产出报告（见下方偏离说明）

**依赖：** T23
**文件：** `tests/benchmark/*.py`、`docs/benchmark.md`
**规模：** S

**⚠️ 与本条目原文的偏离：没有用 pytest-benchmark**（`--benchmark-only` 是它的
参数）。理由有二：它面向同步微基准，会对同一个函数反复校准重跑，而这里每次
测量都带状态（入库会写库、建 collection），重跑会互相污染；它报的是
mean/median/stddev，**没有 P95/P99** —— 而验收标准要的恰恰是后者。
自己写了个三十行的采集器，顺带支持 `--bench-baseline` 做前后对比。

**除一条外都不断言耗时。** 机器一换数字就变，那种用例只会训练出"红了就重跑"
的习惯。回归靠对比发现，是人看的，不是 CI 判的。

唯一带断言的是 `overlap_speedup`，因为它的判据是**结构性**的：给替身加 5ms
延迟，200 段串行需 1000ms，窗口化并发实测 92ms。阈值取 `serial/4`，离实测值
很远。它守的是 T9 修好的那件事 —— 反向验证：把 `_index` 改回逐条 `await`，
实测 1097ms，立刻变红。

**基准跑错了配置，比没有基准更糟。** 第一版怎么算都对不上：`overlap_speedup`
只有 1.74×，而理论值该是 10× 以上。根因是
`tests/e2e/test_ingest_search.py::_use_stub_loader` 会**重新注册** runner，
覆盖掉 `create_app(pipeline_config=...)` 装配的那份 —— 基准以为自己配的是
`32/16`，实际跑的是那里硬编码的 `2/4`。修好后是 10.95×。
现在 `_use_stub_loader` 接受显式 config，并在 docstring 里写明了这个陷阱。

**基线数值**（全内存后端，200 段文档）：单文档入库 P50 19.2ms、
批量吞吐 85 doc/s（17 043 chunk/s）、检索 P50 3.21ms / P95 3.39ms。
全部连同"这些数字不代表真实 QPS"的说明记进 `docs/benchmark.md`。

---

### 🟡 T28 — 清理与文档

**验收标准：**
- [ ] `poc/task_demo/` 删除 —— **待你确认**：该目录被 gitignore、从未进过版本库，
      删掉不可恢复。价值已被 T4–T8 的测试固化，但那是我的判断，不是你的。
- [x] `config/schemas.py` 中未使用的配置类清理干净（删掉 `S3Config`，全仓 0 引用）
- [x] `docs/` 全量校对：新增 `deployment.md`、`architecture.md`、`benchmark.md`
- [x] README 更新：库用法与服务部署两条路径分开写
- [x] 🔴 **统一启动入口**（2026-08-11 记）—— 见下
- [x] `tests/unit` 覆盖率 ≥ 70%（**78%**），`comet_rag/tasks/` ≥ 90%（**96%**）

**验证：**
- [x] `uv run pytest --cov=comet_rag --cov-report=term-missing`
- [x] 按 README 在干净环境走一遍，两条路径都能跑通

**依赖：** T24、T26、T27
**文件：** `README.md`、`docs/*.md`、`comet_rag/cli.py`、`pyproject.toml`
**规模：** S

**启动入口：把歧义变成了可演示的对照。**

    comet-rag serve                  → 0.0.0.0:8100   （config.yaml 里写的）
    uvicorn comet_rag.api.main:app   → 127.0.0.1:8000 （uvicorn 默认）

同一份配置、两条命令、两个结果。根因不是 bug 而是职责划分：host/port 属于
**服务器**、不属于 ASGI 应用，uvicorn 命令行直接拿走 app，配置里那两个值
根本没机会参与。`comet-rag serve` 把"读配置 → 起服务器"合成一条命令；
`test_cli.py` 里有一条用例专门钉住"配置里的 host/port 真的被用上了"。

**顺带解决了两件挂账的事：**
· `pyproject.toml` 补上 `[build-system]`，包才真正装进 venv —— 此前
  `arq comet_rag.workers.…` 换个目录就找不到模块；
· 配置路径可指定了：`--config` > `$COMET_RAG_CONFIG` > `./config.yaml`。
  环境变量这条不是可有可无 —— `--reload` 与 worker 都会另起子进程，
  只有它传得过去。

新增 `comet-rag config` 打印生效配置（密码脱敏）：排查配置问题时先跑它，
省掉"我改的是不是这个文件"那一轮。脱敏有专门的用例守着 ——
排查现场最容易顺手把密码贴进工单。

**README 的示例当天就被新守卫抓到一个错。** `test_docs_examples.py` 原本只扫
`docs/*.md`，把 README 加进去之后立刻红：我写的是
`for chunk in pipeline.run(...)`，而 `run()` 返回的是 `PipelineResult`，
可迭代的是 `result.chunks`。**README 是第一批用户看到的第一段代码**，
却一直不在守卫范围内。

**覆盖率这条标准需要说清楚怎么量。** `comet_rag/tasks/` 只看 unit 是 80%，
因为 `ArqExecutor`（33%）与 `PostgresTaskStore`（28%）天生要中间件。
合并三层后是 **96%**。把"要中间件才能覆盖的代码"算成未覆盖，会推着人去写
mock 掉一切的假单测 —— 那种测试通过率好看，但换个后端立刻失效。
量法与理由写进了 `tests/README.md`。

---

## 规模统计

| 规模 | 数量 | 任务 |
|---|---|---|
| XS | 1 | T3 |
| S | 13 | T1 T2 T6 T9 T10 T11 T12 T17a T24 T25 T26 T27 T28 |
| M | 15 | T4 T5 T7 T8 T13 T14 T15 T16 T17b T18 T19 T20 T21 T22 T23 |
| L+ | **0** | — |
| **合计** | **29** | |

### 关于"单任务不超过 ~5 个文件"

- T17 原本会碰到 8+ 个文件，已拆为 **T17a（资源装配，3 文件）** 与 **T17b（路由接线，5 文件）**。
- **T5 是唯一的例外**（7 个文件）：它是一次整体目录搬迁，6 个文件属于机械移动，真正的智力工作只有"移除确认门"这一件。硬拆成两个任务会让中间态无法编译，反而更糟。

无 L 及以上任务，符合"agent 在 S/M 上表现最好"的约束。
