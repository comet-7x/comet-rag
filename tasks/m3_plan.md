# Implementation Plan: Comet-RAG M3（BM25 + RRF）

> 状态：执行中（v1.0）
> 依据：`tasks/m3_spec.md` v1.0、GitHub Issue #54
> 开始日期：2026-09-11
> 分支：`feature/hybrid-search`

## Overview

M3 在现有 dense 检索和 reranker 之间增加独立 BM25 召回与纯 RRF 融合。Milvus
承担存储、analyzer 与 BM25 计算，Port 隔离供应商；Service 编排通道、融合、重排
与降级。默认仍为 dense，旧 collection 不会被自动删除。

## Current Priority

M3-T5 已完成纯计算 RRF Strategy、通道贡献诊断、性质测试与零基 rank 反向验证。
下一项 M3-T6 在保持默认 dense 兼容的前提下，为 RetrievalService 与 API 增加
dense、keyword、hybrid 三种显式模式。

## Dependency Graph

```text
M3-T1 规格与基线
        │
        ▼
M3-T2 检索 Port 与兼容层
        │
        ▼
M3-T3 Milvus 版本与 BM25 schema
        │
        ▼
M3-T4 KeywordSearch 实现与契约
        │
        ▼
M3-T5 RRF Strategy
        │
        ▼
M3-T6 RetrievalService 与 API
        │
        ▼
M3-T7 降级、配置与组合根
        │
        ▼
M3-T8 真实链路、基准、文档与验收
```

## Schedule

| 日期 | 任务 | Checkpoint |
|---|---|---|
| 09-11 | M3-T1 选型、规格、Issue 与基线 | A：规格冻结 |
| 09-14 | M3-T2 Port 下沉、兼容导出与分层守卫 | B：边界稳定 |
| 09-15 | M3-T3 Milvus 2.6、BM25 schema 与旧库拒绝 | C：存储就绪 |
| 09-16 | M3-T4 InMemory/Milvus keyword search 与契约 | D：双路可召回 |
| 09-17 | M3-T5 RRF Strategy 与性质测试 | E：融合稳定 |
| 09-18 | M3-T6 三模式 Service、API 与响应诊断 | F：用例完成 |
| 09-21 | M3-T7 通道降级、配置与组合根 | G：服务装配完成 |
| 09-22～09-23 | M3-T8 真实集成、E2E、基准、文档与 PR | H：M3 完成 |
| 09-24～09-25 | AI Bot 增量评审与兼容缓冲 | 合并窗口 |

## Checkpoints

- **A — 规格冻结**：后端、版本、schema、迁移、模式、RRF 与降级没有隐含决策。
- **B — 边界稳定**：Services 只依赖 ports；旧导入兼容；core-only 不加载 PyMilvus。
- **C — 存储就绪**：真实 Milvus 验证 analyzer、function、index；旧库不会被误删。
- **D — 双路可召回**：两个实现通过同一关键词检索契约和过滤语义。
- **E — 融合稳定**：RRF 与供应商无关、顺序确定、反向验证有效。
- **F — 用例完成**：dense/keyword/hybrid 均可调用，默认行为兼容。
- **G — 服务装配完成**：单路降级可观察，配置与生命周期正确。
- **H — M3 完成**：真实样本、性能、文档和全质量门通过。

## Pull Request Strategy

M3 使用 `feature/hybrid-search` 单分支、一个面向 `develop` 的 PR，但提交边界必须按
T2～T8 分开。schema 变更只有在 T2 契约稳定并完成真实 Milvus 验证后才进入分支。

## Risks and Mitigations

| 风险 | 影响 | 缓解 |
|---|---|---|
| 2.5.4 服务端与 3.0.1 SDK 错配 | 高 | 固定 Milvus 2.6.23 + PyMilvus 2.6.17 版本线并跑真实契约 |
| 预留 sparse 字段被误认为已支持 BM25 | 高 | schema 检查必须包含 analyzer、Function 与 BM25 metric |
| 自动升级旧 collection 丢数据 | 高 | 只报 `CollectionSchemaMismatch`；删除和重新入库由操作者显式执行 |
| 中文被 standard analyzer 当成长 token | 高 | 使用 chinese analyzer，并以中文术语集成测试验证 |
| dense 与 BM25 原始分数不可比 | 高 | RRF 只用排名，不混加原始分数 |
| hybrid 局部故障拖垮读路径 | 中 | 通道级降级、实际通道与原因写入响应和日志 |
| 新测试突破 10 秒 | 中 | 单元测试全 fake；真实 Milvus 测试保持 integration marker |
| 配置遗漏 database 导致访问默认库 | 高 | 连接前强制显式 `db_name`；本环境仅允许 `zhihao_test_database` |

## Completion Record

M3-T1～T5 的决策与验证结果见 `tasks/m3_spec.md` §6～§10；M3-T8 补最终记录。
