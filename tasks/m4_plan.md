# Implementation Plan: Comet-RAG M4（Chunking 与层级索引）

> 状态：执行中（v1.2）
> 依据：`tasks/m4_spec.md` v1.2
> GitHub Issue：[#57](https://github.com/comet-7x/comet-rag/issues/57)
> 开始日期：2026-09-14
> 分支：`feature/m4-chunking`

## Overview

M4 先解决当前 `list[str]` 契约造成的溯源丢失和 overlap 不可观察，再实现结构感知
策略；父子索引与双存储可靠性置于独立决策门之后。前后两段不能倒序：没有稳定、带位置
的平坦块，`parent_id` 和 DocumentStore 只会把错误永久化。

## Current Priority

M4-T1～T4 已完成：平坦策略保留精确 span，Pipeline 与任务入库已经共用
`ChunkingService`、ChunkDraft 序列化和物化规则。下一项是 M4-T5 Markdown / Page
结构感知；仍不创建数据库表、不改 Milvus metadata。

## Dependency Graph

```text
M4-T1 调研、规格与基线
        │
        ▼
M4-T2 ChunkDraft / Strategy 契约
        │
        ▼
M4-T3 Fixed + Recursive 核心
        │
        ▼
M4-T4 Pipeline / Task 单链路迁移
        │
        ▼
M4-T5 Markdown / Page 结构感知
        │
        ▼
M4-T6 IndexPlan + schema/可靠性设计门
        │ 需要明确确认
        ▼
M4-T7 DocumentStore 契约与实现
        │
        ▼
M4-T8 revision 双存储入库
        │
        ▼
M4-T9 父块回填与检索兼容
        │
        ▼
M4-T10 评测、文档、完整验收与 PR
```

## Schedule

日期是顺序预算，不是以赶日期为由跳过 Checkpoint 的承诺。

| 日期 | 任务 | Checkpoint |
|---|---|---|
| 09-14 | M4-T1 调研、现状审计、规格与基线 | A：方向可评审 |
| 09-15 | M4-T2 ChunkDraft、Strategy、位置契约 | B：输出稳定 |
| 09-16～09-17 | M4-T3 Fixed/Recursive 与 separator profiles | C：平坦算法正确 |
| 09-18 | M4-T4 Pipeline/Task 单链路迁移与兼容 | D：入口一致 |
| 09-19～09-20 | M4-T5 Markdown/Page 结构感知 | E：结构不丢 |
| 09-21 | M4-T6 IndexPlan、schema、revision 故障矩阵 | F：人工决策门 |
| 09-22～09-23 | M4-T7 DocumentStore 契约、内存与 PostgreSQL | G：父块可存取 |
| 09-24～09-25 | M4-T8 revision 双存储入库 | H：写路径可靠 |
| 09-26 | M4-T9 父块回填、检索兼容与降级 | I：读路径闭环 |
| 09-27～09-28 | M4-T10 真实链路、评测、文档与 PR | J：M4 完成 |
| 09-29～09-30 | AI Bot 增量评审与缓冲 | 合并窗口 |

## Checkpoints

- **A — 方向可评审**：开源对照、现状缺陷、采用与拒绝项均有依据。
- **B — 输出稳定**：Strategy 输入文档、输出带位置 ChunkDraft；async 和 ID 不混入算法。
- **C — 平坦算法正确**：固定/递归通过性质测试，位置不靠搜索恢复。
- **D — 入口一致**：Pipeline 与任务入库共用映射，旧 hook 有兼容测试。
- **E — 结构不丢**：标题路径和真实页边界进入 metadata，缺页时不猜。
- **F — 人工决策门**：表结构、向量 metadata、revision 激活和失败回滚已逐项确认。
- **G — 父块可存取**：两个 DocumentStore 实现通过同一契约和隔离测试。
- **H — 写路径可靠**：任一写入断点失败都不会替换当前可用 revision。
- **I — 读路径闭环**：子块召回后可回填父块；失败按规格降级且可观察。
- **J — M4 完成**：质量数据、性能、兼容、文档和全部质量门通过。

## Pull Request Strategy

前半段 T1～T5 可在 `feature/m4-chunking` 上形成一个聚焦 Chunking 的 PR。如果 T6
确认引入 PostgreSQL DocumentStore 与 revision 协议，后半段应使用新的
`feature/m4-hierarchical-indexing` 分支和独立 PR。数据库迁移、向量 metadata 与检索
回填不压进基础算法 PR，便于独立回滚。

## Risks and Mitigations

| 风险 | 影响 | 缓解 |
|---|---|---|
| 把 `list[str]` 换成巨型万能对象 | 高 | ChunkDraft 只含切分事实，ID/embedding/存储状态留给 Service |
| 通过 `str.find` 回查位置 | 高 | 内部 split 单元始终携带 span；重复文本反向测试 |
| token size 与字符位置混为一谈 | 高 | LengthFunction 只计预算，位置始终是 Markdown 字符 offset |
| 语义分块绕过模型闸门 | 高 | Service 复用 EmbeddingPort 与批量排程；engines 只算断点 |
| 页面边界由 Chunker 猜测 | 中 | PageChunker 只接受 DocumentBlock page fact，缺失时显式失败/回退 |
| 父子索引造成双存储半写 | 高 | revision 协议、逐断点故障矩阵、T6 人工决策门 |
| 一次 PR 难以评审和回滚 | 高 | T1～T5 与 T6～T10 分支/PR 分离 |
| 质量优化只看块长 | 中 | 固定样本同时记录边界、hit@k、延迟与索引体积 |
| 新单测突破 10 秒 | 中 | 性质测试小样本，质量/真实存储进入 benchmark/integration marker |
