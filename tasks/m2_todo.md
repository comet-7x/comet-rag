# TODO: Comet-RAG M2（PDF / MinerU HTTP）

> 状态：M2-T1～M2-T9 已完成。宏观排期见 `tasks/m2_plan.md`
> 规格：`tasks/m2_spec.md` v1.0
> 长期架构：`tasks/architecture_plan.md`；其中 Loader/DOCX 重构不属于当前 M2 工作

## Phase 0：决策与保护网

### M2-T1 — 锁定 HTTP-only 与基线（S）

**日期：** 09-10

- [x] 确认只支持外部 `mineru-api` / `mineru-router`
- [x] M2 分支同步最新 `develop`
- [x] 删除 `mineru` extra 并刷新 `uv.lock`
- [x] 修正项目规格、M1 历史编号和 M2 wire contract
- [x] 删除进程内 SDK/CLI 文档，只保留外部 HTTP 架构
- [x] 建立 M2 宏观计划、微观 TODO 与 Checkpoint
- [x] `uv run pytest -q` 小于 10 秒（1580 passed，8.61s）

**验收：** 默认、`all` 与 dev 安装图中均没有 MinerU SDK、Torch 或模型权重。

## Phase 1：提取契约与引擎扩展点

### M2-T2 — DocumentExtractorPort 与错误词汇表（S）

**日期：** 09-11　**依赖：** M2-T1

- [x] 新增 `ExtractedDocument` 与 `DocumentExtractorPort`
- [x] 明确协议错误、资源超限、可重试上游错误的类型边界
- [x] 同步/异步假实现通过同一组行为断言
- [x] Port 不引用 `engines`、httpx 或 MinerU 字段

**验收：** 全量 `1595 passed`，8.20s；Ruff、格式与 Pyright 均通过。

**主要文件：** `comet_rag/ports/document.py`、`comet_rag/ports/__init__.py`、单元测试。

### M2-T3 — PipelineHooks 异步提取（M）

**日期：** 09-14　**依赖：** M2-T2

- [x] 保留同步 extractor 注册 API，新增异步注册与查找
- [x] `Pipeline.arun()` 和 `IngestRunner` 优先异步；同步 fallback 只进一次线程
- [x] 拆分 Pipeline 的提取、分块与结果构造，四种入口行为一致
- [x] DOCX 快照与已有 Hook 隔离测试不变

**验收：** 全量 `1605 passed`，8.26s；Ruff、格式与 Pyright 均通过。

**主要文件：** `engines/pipelines/hooks.py`、`pipeline.py`、`services/ingestion.py`、对应测试。

## Phase 2：MinerU HTTP 适配器

### M2-T4 — 提交、轮询与 Markdown 结果（M）

**日期：** 09-15～09-16　**依赖：** M2-T2、M2-T3

- [x] health 校验状态和协议版本
- [x] 流式上传单个 PDF，显式发送全部产物开关
- [x] 处理 pending / processing / completed / failed
- [x] 从唯一 `results.*.md_content` 生成稳定结果
- [x] 同步与异步入口语义一致并复用各自 client

**验收：** 全量 `1633 passed`，8.62s；Ruff、格式与 Pyright 均通过。

**主要文件：** `infrastructure/providers/document/mineru.py`、导出文件、MockTransport 测试。

### M2-T5 — 容错、取消与资源上限（M）

**日期：** 09-17　**依赖：** M2-T4

- [x] 429、5xx、网络错误映射为可重试错误；确定性 4xx 直接失败
- [x] 远端 404 在总 deadline 内最多重提一次
- [x] 区分连接、上传、单次请求和总解析超时
- [x] 响应体与 Markdown 分别限长，写 Task context 前复验
- [x] 取消时停止轮询并关闭响应流；内部 client 正确关闭，注入 client 不关闭
- [x] 轮询单测注入 sleeper/clock，不使用真实 sleep

**验收：** MinerU/ingestion 定向 `72 passed`；全量 `1661 passed`、20 skipped、
171 deselected、1 xfailed，8.63s；Ruff、Pyright 与分层守卫通过。

## Phase 3：服务装配

### M2-T6 — 配置、独立闸门与生命周期（M）

**日期：** 09-18　**依赖：** M2-T5

- [x] 新增 MinerU provider 配置与跨字段校验，`enabled=false` 为安全默认
- [x] 新增独立并发、队列和等待预算，不复用 loader/model 闸门
- [x] 组合根注册 PDF 同步/异步 Hook，具体适配器不泄漏到 services/engines
- [x] 将 PDF 的 Markdown 限制注入 IngestRunner，启用 Task context 写入前复验
- [x] Context 逆序关闭 MinerU 资源，重复关闭幂等
- [x] 配置禁止未托管 headers，输出不泄漏凭据或内部请求头

**验收：** 全量 `1684 passed`、20 skipped、171 deselected、1 xfailed，8.79s；
Ruff、格式、Pyright、闸门入口行为守卫与分层守卫通过。

## Phase 4：端到端与出口验收

### M2-T7 — Local / URL / S3 PDF 入库（M）

**日期：** 09-21　**依赖：** M2-T6

- [x] 三种来源经 Loader 后走同一个 DocumentExtractorPort
- [x] 提取前用内容检测复验 PDF，伪造后缀必须拒绝
- [x] Task 阶段、重试、取消和断点续跑只通过 TaskStore 观察
- [x] `/search` 命中正文、表格和公式文本；DOCX E2E 不回退

**验收：** PDF 定向 `3 passed`，三来源与 DOCX E2E `5 passed`；完整 E2E
`29 passed`；全量 `1686 passed`、20 skipped、176 deselected、1 xfailed，8.48s；
Ruff、格式、Pyright 与 414 条分层守卫均通过。

### M2-T8 — 真实 MinerU 集成与基准（M）

**日期：** 09-21　**依赖：** M2-T7

- [x] 未设置 `COMET_TEST_MINERU_URL` 或服务不可达时 skip
- [x] 文本 PDF 与扫描 PDF 各跑一次真实 `/tasks` 链路
- [x] 记录耗时、输出字节、峰值内存与 CPU lane 占用
- [x] 根据数据确认并发、超时和大小默认值

**验收：** 2026-09-10 使用 MinerU 3.4.5 `mineru-api`（protocol v2）连接远端
`opendatalab/MinerU2.5-2509-1.2B` vLLM，真实测试 `1 passed in 3.50s`。文本 PDF
584 B → Markdown 9 B，端到端 2.10s；扫描 PDF 1559 B → Markdown 14 B，端到端
3.11s；两者均生成 1 个 chunk 并经 `/search` 命中。Comet-RAG 测试进程 Python
堆峰值 1,450,128 B，本地 MinerU 闸门峰值 2；CPU lane 持有占端到端时间
99.56% / 99.66%。样本仅用于打通边界而非估计长文档尾延迟，因此保留并发 2、
总超时 900s、响应 16 MiB、Markdown 8 MiB 的保守默认值，不以小样本下调上限；
生产部署按上游容量调整并发。完整原始数据由测试的 `--mineru-report` 出口生成。

### M2-T9 — 文档、全量验证与收尾（S）

**原计划：** 09-22　**实际完成：** 09-10　**依赖：** M2-T8

- [x] 根据最终实现补全 MinerU 集成文档，覆盖 `mineru-api` 与 `mineru-router`
- [x] 更新配置示例、部署、结构与 Pipeline 用法
- [ ] M2 规格成功标准全部勾选，Issue #50 与 PR 描述同步
- [x] ruff、pyright、core-only、unit、integration、e2e 全部通过
- [x] 默认 `uv run pytest` 小于 10 秒

**本地验收：** Ruff、格式检查和 Pyright 通过；隔离 core-only 环境的 engines
测试 `362 passed, 1 skipped, 1 xfailed`；默认单测 `1687 passed, 19 skipped,
1 xfailed`，pytest 计时 8.71s；integration `6 passed, 136 skipped`；e2e
`29 passed`。未启动的中间件与真实 MinerU 用例按约定 skip。

## 规模与提交

| 规模 | 任务 |
|---|---|
| S | M2-T1、M2-T2、M2-T9 |
| M | M2-T3～M2-T8 |
| L | 无；发现 L 任务必须继续拆分 |

每个任务至少一个 Conventional Commit；不得用删除失败测试换取绿色 CI。
