# TODO: Comet-RAG M3（BM25 + RRF）

> 状态：M3-T7 已完成，下一项 M3-T8
> 规格：`tasks/m3_spec.md` v1.0
> 计划：`tasks/m3_plan.md` v1.0
> GitHub Issue：[#54](https://github.com/comet-7x/comet-rag/issues/54)

## Phase 0：决策与保护网

### M3-T1 — 选型、规格与基线（S）

**日期：** 09-11

- [x] 从 PR #53 合并后的最新 `develop` 创建 `feature/hybrid-search`
- [x] 选择 Milvus 原生 BM25，不增加新中间件
- [x] 冻结 Milvus 2.6.23 + PyMilvus `>=2.6.17,<2.7` 版本线
- [x] 明确旧 collection 不自动迁移，必须显式重建
- [x] 冻结 KeywordSearchPort、RRF、查询模式、过滤和降级语义
- [x] 记录 `1786 passed`、9.38s、Ruff/Pyright 全绿基线
- [x] 建立 M3 规格、计划、TODO 与 GitHub Issue

**验收：** 规格不再把“存在 sparse 字段”误写成“已经支持 BM25”；真实 Milvus
验证明确留在版本对齐后的 M3-T3，离线 schema 构造不冒充集成测试。

## Phase 1：契约与存储

### M3-T2 — 检索 Port 下沉与兼容层（M）

**完成日期：** 09-11　**依赖：** M3-T1

- [x] 新增后端无关的 `VectorSearchPort` 与检索值对象
- [x] `KeywordSearchPort` 延后到 M3-T4，与调用者、两个实现和契约测试同时引入
- [x] 将 `BaseVectorStore` 及写入词汇表下沉 `ports/`，已有签名保持不变
- [x] `infrastructure.vectorstore.base` 保留同一对象的兼容导出
- [x] RetrievalService 不再 import infrastructure
- [x] 增加 AST 分层守卫、兼容导入测试与反向验证

**验收：** core-only 可导入全部契约且不加载 PyMilvus；现有 27 条向量库契约不回退。

### M3-T3 — Milvus 2.6 与 BM25 schema v2（M）

**完成日期：** 09-11　**依赖：** M3-T2

- [x] Compose 固定 Milvus 2.6.23，PyMilvus 固定 `>=2.6.17,<2.7`
- [x] 配置、组合根与 Milvus client 显式传递 database name，不允许回落默认库
- [x] 本环境真实验证只使用 `.env` 的 URI，并强制 database=`zhihao_test_database`
- [x] text 启用 chinese analyzer，注册 BM25 Function
- [x] sparse index metric 改为 BM25，写入不再提供空 sparse dict
- [x] 检测旧 schema 并抛 `CollectionSchemaMismatch`，绝不自动 drop
- [x] 在真实 Milvus 2.6 服务验证版本、analyzer、建库、写入和 BM25 查询

**验收：** 中文词、英文标识符均可命中；故意注入旧 schema 时明确失败且数据仍存在。

### M3-T4 — KeywordSearchPort 实现与契约（M）

**完成日期：** 09-11　**依赖：** M3-T3

- [x] 引入 `KeywordSearchPort`，并由组合根显式注入 RetrievalService
- [x] InMemoryVectorStore 实现确定性关键词召回
- [x] MilvusStore 使用原始 query text 检索 sparse field
- [x] 两个实现共享知识库隔离、top_k、filter、空结果、排序和错误契约
- [x] 契约期望值独立手写，并反向注入过滤缺陷验证

**验收：** 替换实现不改变 Port 行为，Milvus 专有参数不穿透接口。

## Phase 2：融合与用例编排

### M3-T5 — RRF Strategy（S）

**完成日期：** 09-11　**依赖：** M3-T4

- [x] 实现默认 `rrf_k=60`、rank 从 1 开始的 RRF
- [x] 覆盖去重、缺失通道、并列、输入不变和非法参数
- [x] 保留各通道 rank/score，融合排序同分时以 id 稳定化
- [x] 反向将 rank 改为从 0 开始，确认精确公式测试会红

**验收：** Strategy 为纯计算，core-only 可用，不依赖 Milvus。

### M3-T6 — RetrievalService 与 API 三模式（M）

**完成日期：** 09-11　**依赖：** M3-T5

- [x] `SearchQuery.mode` 支持 dense/keyword/hybrid，默认 dense
- [x] 每路使用同一有界 `fetch_k`，hybrid 先融合再 rerank
- [x] 响应增加实际模式、通道和分项分数，旧字段不删除
- [x] API、服务单测覆盖三模式和默认兼容

**验收：** 精确术语与语义改写样本可由显式模式稳定检索。

### M3-T7 — 降级、配置与组合根（M）

**完成日期：** 09-12　**依赖：** M3-T6

- [x] hybrid 单路失败降级到另一通道，两路失败才报错
- [x] schema/参数/知识库错误不误判为可降级故障
- [x] 结果与日志记录实际通道和原因
- [x] 组合根注入窄 Port，路由不 new 资源，生命周期不重复关闭

**验收：** 降级行为从 Service 返回值可观察，不依赖日志猜测。

## Phase 3：真实链路与出口验收

### M3-T8 — 集成、E2E、基准、文档与 PR（M）

**日期：** 09-22～09-23　**依赖：** M3-T7

- [ ] 真实 Milvus 仅在 `zhihao_test_database` 覆盖中文术语、英文标识符、过滤与旧 schema 拒绝
- [ ] collection 使用测试专属名称且只清理本次创建的数据
- [ ] E2E 覆盖 dense/keyword/hybrid 与 reranker 降级
- [ ] 对固定样本记录命中、延迟、候选数，不夸大质量结论
- [ ] 更新 README、architecture、structure、deployment 和 API 示例
- [ ] unit < 10s，core-only、integration、e2e、Ruff、Pyright 全绿
- [ ] 创建面向 `develop` 的 PR，完成 AI Bot 增量评审

**验收：** `tasks/m3_spec.md` S1～S5 全部完成，Issue 与文档状态一致。

## 规模与提交

| 规模 | 任务 |
|---|---|
| S | M3-T1、M3-T5 |
| M | M3-T2～T4、M3-T6～T8 |
| L | 无；发现 L 任务必须继续拆分 |

每个任务至少一个 Conventional Commit；不得把 Port 迁移、schema 变更和 Service
编排压成一个不可评审提交。
