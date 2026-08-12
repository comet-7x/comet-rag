# Implementation Plan: Comet-RAG M1（DOCX 全链路）

> 依据：`tasks/spec.md` v0.1（A1–A12）
> 范围：仅 M1。M2（MinerU/PDF）、M3（混合检索）不在本计划内
> 最后更新：2026-08-09

---

## Overview

把 Comet-RAG 从"6600 行零测试的引擎集合"推进到"一条可部署、有回归保护的 DOCX 入库与检索链路"。

M1 结束时应当满足：上传 docx → 异步任务解析分块 → 向量化 → 写入 Milvus → 检索命中，全程可查进度、可重试、可跨进程扩容；同时 `pip install comet-rag`（不带 extras）仍能单独跑引擎。

---

## Architecture Decisions

以下决策已在 spec 中锁定，此处只记与**实施顺序**相关的推论：

1. **先内存后真实（本计划最重要的决策）**
   `TaskStore` / `TaskExecutor` / `BaseVectorStore` 三个抽象各有内存实现。因此可以在**零外部依赖**的情况下先打通完整端到端链路（Phase 3），再逐个换成 Postgres / Milvus / ARQ（Phase 4–5）。
   收益：最大的架构风险（"这套抽象到底成不成立"）在不装任何中间件的情况下就被证伪或证实；此后每替换一个后端，端到端测试始终在保护你。
2. **契约测试是抽象的唯一验收手段**
   每个 ABC 配一套与实现无关的契约测试，所有实现跑同一套。`InMemoryXxx` 不是玩具——它是验证抽象没有绑死在某个后端上的工具。
3. **POC 迁移前先固化行为**
   `poc/task_demo/demo.py` 的 5 个场景已经是事实上的测试（乐观锁冲突、非法迁移、租约回收、序列化往返、协作式取消），只是用 `print` 写的。**先把它转成 pytest 断言，再搬代码**，否则迁移中的行为漂移无人察觉。
4. **高风险任务前置**
   `docx_parser.py`（962 行零测试）和 Milvus 写入可见性语义是两个最可能翻车的点，分别安排在 T11、T20，都在各自阶段的早期。

---

## Dependency Graph

```
T1 测试基建 ──┬─► T2 依赖分组(A1)
              │
              ├─► T4 demo→pytest ─► T5 tasks/ 提升+去确认门 ─┬─► T6 状态机测试
              │                                              ├─► T7 TaskStore 契约
              │                                              └─► T8 Executor 契约
              │
              └─► T9  批量化(S4-3) ──┐
                  T10 连接池(S4-4)   ├─► T12 chunkers 测试
                  T11 hooks 隔离     │   T13 docx_parser 快照
                                     │
                    ┌────────────────┘
                    ▼
        T14 VectorStore 接口 + InMemory
                    │
                    ├─► T15 ingestion runner ─┐
                    └─► T16 retrieval service ─┼─► T17 API + lifespan（全内存）
                                               │
                              ══ Checkpoint C：端到端跑通，零中间件 ══
                                               │
              ┌────────────────────────────────┘
              ▼
        T18 docker-compose + database + alembic
              ├─► T19 knowledge_bases 表 + KB service
              ├─► T20 PostgresTaskStore  （过 T7 契约）
              └─► T21 MilvusStore        （过 T14 契约）
                              ══ Checkpoint D：真实后端全部接上 ══
              ▼
        T22 ArqExecutor（过 T8 契约） ─► T23 workers ─► T24 sweep_stale
                              ══ Checkpoint E：跨进程与崩溃恢复 ══
              ▼
        T25 并发闸门 ─ T26 分级降级 ─ T27 benchmark ─ T28 清理与文档
                              ══ Checkpoint F：M1 完成 ══
```

**关键路径**：T1 → T5 → T14 → T15 → T17 → T21 → T23。其余任务可在依赖满足后并行。

---

## Task List

### Phase 0：地基（无回归保护，先建保护网）

- [X] T1 — 测试基建与 CI 骨架
- [X] T2 — pyproject 依赖分组，强制执行 A1
- [X] T3 — 修复 `docs/pipeline_usage.md`（示例代码当前照抄即报错）

**Checkpoint A** ✅ 2026-08-09

- [X] `uv run pytest` 可运行 —— 145 passed / 1.06s
- [X] `uv run ruff check` 与 `ruff format --check` 通过
- [X] A1 由 `tests/unit/test_layering.py` 自动守卫（AST 检查，非 grep）
- [X] 仅核心依赖环境下 `from comet_rag.engines.pipelines import Pipeline` 成功；
  `import comet_rag.api.main` 如预期失败（证明隔离是真的）
- [X] 两个守卫经过反向验证：注入违规后确实变红

> **超出原计划的产出**：原计划只要求 grep 式的一次性检查，实际做成了两个常驻测试
> —— `test_layering.py`（AST 分层守卫）与 `test_docs_examples.py`（文档防腐守卫）。
> 后者能自动拦截 P2 那一整类问题（文档引用不存在的符号 / 错误的 import 路径）。

---

### Phase 1：任务框架落地（纯逻辑，零外部依赖）

- [X] T4 — 把 `poc/task_demo/demo.py` 的 5 个场景转成 pytest
- [X] T5 — `poc/task_demo/task/` 提升为 `comet_rag/tasks/`，改名 `StagePipeline`，移除确认门（A10）
- [X] T6 — `states.py` 状态机全矩阵测试
- [X] T7 — `TaskStore` 契约测试套件（`InMemoryTaskStore` 先过）
- [X] T8 — `TaskExecutor` 契约测试套件（`InProcessExecutor` 先过）

**Checkpoint B** ✅ 2026-08-10

- [X] `comet_rag/tasks/` 覆盖率 91%（含分支）。states 100% / store 98% / service 96% / runner 94%
- [X] 确认门已移除，断点续跑改由失败驱动并经测试验证（`s1,s2,s3,s3`）
- [X] 全套 321 用例 / 1.9s，连跑 6 次零 flaky
- [ ] `poc/task_demo/` **未删除**：该目录被 `.gitignore` 整体忽略、从未进过版本库，
  删除不可恢复，留给开发者处置。注意其中 `demo.py` 已失效（依赖已移除的确认门）

> **已知覆盖缺口**：`executor.py` 停在 81%（分支）。剩余未覆盖的是防御性路径——
> 关停超时硬掐、`_on_done` 的兜底异常记录、跨线程 `cancel` 探测。触发它们需要
> 制造竞态或多线程环境，硬凑会写出 flaky 测试，故不追这个数字。

---

### Phase 2：引擎修复（还 S4 的技术债）

- [X] T9 — `astream_run` 改窗口化并发（P1 / S4-3，验收标准已修正）
- [X] T10 — `url_loader` 复用 httpx client（P2b / S4-4）
- [X] T11 — `PipelineHooks` 测试隔离（P8）
- [X] T12 — chunkers 单元测试
- [X] T13 — `docx_parser` 快照测试

**Checkpoint C-1** ✅ 2026-08-10

- [X] S4-3 验证通过（**标准已修正**）：并发峰值 1 < peak ≤ max_concurrency；
  修复前恒为 1。原"调用数 ≤ 7"的标准基于不存在的请求级批量，见 spec S4-3 修正记录
- [X] S4-4 验证通过：全仓 4 处 client 创建点，全部为懒创建实例级或注入式带兜底
- [X] `tests/unit` 全套 559 用例 / 7.0s（< 10s）
- [X] 引擎侧覆盖率：chunkers 96%、parsers 86%（docx_parser 82%、omml 92%）

---

### Phase 3：第一条端到端切片（全内存后端，零中间件）★

本阶段是全计划的价值高点：不装任何中间件就验证整套抽象是否成立。

- [X] T14 — `BaseVectorStore` 接口重设计 + `InMemoryVectorStore` + 契约测试
- [X] T15 — `services/ingestion.py`：注册 `kind="ingest"` 的多阶段 runner
- [X] T16 — `services/retrieval.py`：召回 + rerank
- [X] T17a — `Context` 与 `lifespan` 资源装配（全部注入内存实现）
- [X] T17b — API 路由接线

**Checkpoint C：端到端跑通（内存版）** ✅ 2026-08-11

- [X] `tests/e2e/test_ingest_search.py` 12 个用例：`POST /kb` → `POST /ingest` →
  轮询 `GET /tasks/{id}` 见 stage 推进 → `SUCCEEDED` → `POST /search` 命中
- [X] **不需要 docker**，纯内存跑完（1.9s）
- [X] chunk metadata 携带 `kb_id`；跨知识库检索不串数据
- [X] 相同 `idempotency_key` 重复提交不产生第二个任务
- [X] `/tasks/{id}` 不外泄 traceback / worker_id / version / context
- [ ] **人工评审**：抽象是否真的成立？← 等开发者确认

> **抽象经受住了考验**：三个 ABC（TaskStore / TaskExecutor / BaseVectorStore）
> 在零中间件下拼出了完整链路，没有为了跑通而回头改任何接口。
> e2e 只经公开 HTTP API 观察，Phase 4 换后端时应当一字不改地继续通过。

---

### Phase 4：换上真实后端（每次只换一个）

- [X] T18 — `docker-compose.yml` + `infrastructure/database/` + alembic 初始化
- [X] T19 — `knowledge_bases` 表 + `services/knowledge_base.py`（A12）
- [X] T20 — `PostgresTaskStore`（通过 T7 的 37 条契约，一行未改）
- [X] T21 — `MilvusStore`（通过 T14 的 27 条契约，一行未改）

**Checkpoint D** ✅ 2026-08-11

- [X] e2e 在真实后端下**只改配置**即通过：`test_e2e_postgres.py`（4 条）
  与 `test_e2e_full_stack.py`（4 条，Postgres + Milvus 同时真实），
  断言与全内存版一字不差
- [X] 契约对 InMemory 与真实实现结果一致：TaskStore 37 条、VectorStore 27 条、
  KB 仓储 10 条，各自两种实现都跑过
- [X] Milvus collection 含预留的 sparse vector 字段（A11，实测已验证 schema）
- [X] 维度不符报 `DimensionMismatch`；**同维度换模型**报 `EmbeddingModelChanged`
  —— 后者是维度检查拦不住的那一半（A12）
- [X] 三层测试：unit 753（零依赖 10.8s）、e2e 17（全内存 2.3s）、
  integration 92（真实中间件 73s）

> **"先内存后真实"这条策略在此完成验证**：三个抽象（TaskStore /
> TaskExecutor / BaseVectorStore）加上 KB 仓储，换后端时业务代码与测试断言
> 一行未动。Phase 3 花在内存实现上的时间，在这里全部收回。

---

### Phase 5：跨进程与容错

- [X] T22 — `ArqExecutor`（必须通过 T8 的同一套契约测试）
- [X] T23 — `workers/preprocessor.py` 与 `workers/embedder.py`
- [x] T24 — `sweep_stale` 定时器与崩溃恢复

**Checkpoint E**

- [x] `kill -9` 掉 worker，任务在租约超时内退回 PENDING 并被另一 worker 接管完成
- [X] preprocessor 用多进程扩容、embedder 用单进程高并发（不得反过来）
- [x] 优雅关停：`SIGTERM` 后在跑的任务能落到一致状态，不留 RUNNING 僵尸

---

### Phase 6：资源治理与收尾

- [x] T25 — 全链路并发闸门与有界背压（S4-1、S4-2）
- [ ] T26 — 分级降级（S4-5）
- [ ] T27 — benchmark 基线（S4-6）
- [ ] T28 — 清理 `schemas/task.py` 残留、更新文档

**Checkpoint F：M1 完成**

- [ ] spec §8 的 S1–S5 全部勾选
- [ ] `tests/unit` 覆盖率 ≥ 70%，`comet_rag/tasks/` ≥ 90%
- [ ] README 与 `docs/` 与实现一致
- [ ] 可以开 M2

---

## Risks and Mitigations

| #  | 风险                                                                             | 影响 | 缓解                                                                                    |
| -- | -------------------------------------------------------------------------------- | ---- | --------------------------------------------------------------------------------------- |
| R1 | **Milvus 写入后不 flush 查不到** —— 内存实现测试全过，换 Milvus 静默失败 | 高   | T14 的契约测试**必须**包含"写入后立即查询"用例；T21 在 Phase 4 早期，留出返工余地 |
| R2 | **`docx_parser.py` 962 行零测试**，任何改动都可能无声破坏解析质量        | 高   | T13 先做快照测试再动它；M1 内**不重构**该文件，只加测试                           |
| R3 | **抽象泄漏** —— filter 语法等 Milvus 专有概念穿透接口                    | 中   | 两个实现跑同一套契约测试；spec 已列入 Never 清单                                        |
| R4 | **POC 迁移行为漂移** —— 搬运时手滑改坏乐观锁/租约逻辑                    | 中   | T4 先把 demo 转成 pytest，T5 搬运后这套测试必须仍绿                                     |
| R5 | **ARQ 生态弱**（无 Flower 级监控），出问题难排查                           | 中   | `TaskExecutor` ABC 隔离，换 Celery 是一天的活；先靠 `TaskEvent` 事件流自建可观测性  |
| R6 | **Phase 3 暴露抽象设计错误**，需返工 Phase 1 的接口                        | 中   | 这正是 Phase 3 前置的目的——此时返工只需改代码，Phase 4 之后返工要动数据               |
| R7 | **覆盖率目标拖慢进度**                                                     | 低   | 分层要求：`tasks/` ≥90%，整体 ≥70%，`poc/` 不作要求                               |
| R8 | **单人开发，上下文跨会话丢失**                                             | 低   | 本计划与`todo.md` 落盘；每个 Checkpoint 是天然的会话边界                              |

---

## Parallelization

单人开发时按顺序做即可。若并行：

- **可并行**：T9/T10/T11 互不相干；T12/T13 独立；T19/T20/T21 在 T18 之后可并行
- **必须串行**：T4 → T5（先固化行为再搬迁）；T14 → T15/T16（接口先定）；T18 → T19/T20（迁移链路先通）
- **需先定契约**：T7、T8、T14 三套契约测试是后续实现的验收标准，必须先于对应实现完成

---

## Open Questions

无阻塞项。实施中出现的新问题追加至此并同步回 `tasks/spec.md`。
