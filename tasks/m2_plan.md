# Implementation Plan: Comet-RAG M2（PDF / MinerU HTTP）

> 状态：实施中（v0.3）
> 依据：`tasks/m2_spec.md` v0.3、GitHub Issue #50
> 目标完成：2026-09-22；评审缓冲至 2026-09-24
> 范围：只连接外部 `mineru-api` / `mineru-router`，不嵌入 MinerU SDK

## Overview

M2 在不改变 M1 任务、存储与检索契约的前提下，让本地、URL 和 S3 来源的 PDF
经过外部 MinerU 服务提取 Markdown，再复用现有分块、向量化和入库链路。

长期 Port / Strategy / Service 边界、Loader 统一入口与目标目录形态见
[`tasks/architecture_plan.md`](architecture_plan.md)。该计划不扩大 M2 范围。

## Current Priority

M2-T6 已完成。当前只执行 **M2-T7**：验证 Local、URL、S3 三种 PDF 来源经过
同一个提取 Port 入库，补提取前 PDF 内容复验，并从 TaskStore 观察阶段、重试、
取消与断点续跑。真实 MinerU 环境和性能数据仍留给 T8。

Loader 统一和 DOCX 提取器迁移已记录为 M2 后 P1，不在此刻移动约 2,000 行 Loader
代码。目录美化不能优先于一个可能无界轮询的外部服务调用。

## Architecture Decisions

1. **HTTP-only**：Comet-RAG 只实现协议适配器；MinerU 进程、GPU、模型与扩容独立部署。
2. **契约先行**：先冻结提取 Port、错误分类与 MinerU HTTP wire contract，再写适配器。
3. **加载与提取分离**：Loader 只把三种来源规范化为受管本地文件；Extractor 只处理文件。
4. **异步优先、同步等价**：服务走 `aextract()`；库用户仍可通过 `Pipeline.run()` 同步调用。
5. **中间态有界**：PDF、HTTP 响应和 Markdown 分别限流；大结果不得直接灌入 Task context。
6. **兼容优先**：DOCX hook 与快照不变；不修改 TaskStore、TaskExecutor 或向量库 schema。
7. **内部按依赖分层、外部统一入口**：Loader 实现允许分属 engines/infrastructure；
   M2 后通过 `comet_rag.loaders` 门面解决使用者发现性，不把可选 S3 SDK 塞进 engines。

## Dependency Graph

```text
M2-T1 决策与基线 ─┬─► M2-T2 提取 Port ───────┐
                  └─► M2-T3 异步 Hook ───────┤
                                              ▼
                              M2-T4 HTTP 适配器主路径
                                              │
                              M2-T5 容错与资源边界
                                              │
                              M2-T6 配置、闸门与装配
                                              │
                              M2-T7 三来源入库链路
                                              │
                              M2-T8 真实集成与基准
                                              │
                              M2-T9 文档与验收
```

关键路径：M2-T1 → M2-T2 → M2-T4 → M2-T5 → M2-T6 → M2-T7 → M2-T8 → M2-T9。
M2-T3 可在 M2-T2 后半段并行准备，但合入前必须基于同一提取契约。

## Schedule

| 日期 | 任务 | Checkpoint |
|---|---|---|
| 09-10 | M2-T1 决策、规格、依赖与基线 | A：规格冻结 |
| 09-11 | M2-T2 提取 Port 与契约测试 | |
| 09-14 | M2-T3 异步 Hook 与 DOCX 回归 | B：引擎扩展点完成 |
| 09-15～09-16 | M2-T4 MinerU HTTP 主路径 | |
| 09-17 | M2-T5 重试、取消、超时与大小限制 | C：适配器契约全绿 |
| 09-18 | M2-T6 配置、独立闸门、组合根与关闭顺序 | D：服务装配完成 |
| 09-21 | M2-T7 三来源 E2E；M2-T8 真实服务与基准 | E：真实链路完成 |
| 09-22 | M2-T9 文档、全量验收与 PR 收尾 | F：M2 完成 |
| 09-23～09-24 | 机器人增量评审与上游兼容缓冲 | 合并窗口 |

## Checkpoints

- **A — 规格冻结**：HTTP-only、wire contract、资源预算和非目标不再含糊。
- **B — 引擎扩展点完成**：同步/异步提取行为一致，DOCX 快照零变化。
- **C — 适配器契约全绿**：MockTransport 覆盖所有状态、重试与资源释放路径。
- **D — 服务装配完成**：配置脱敏、独立闸门、注入与逆序关停均有测试。
- **E — 真实链路完成**：Local/URL/S3 PDF 均可入库并检索命中。
- **F — M2 完成**：规格成功标准全勾选，默认单测仍小于 10 秒。

## Pull Request Strategy

建议拆成三个按顺序合入 `develop` 的 PR，降低评审噪声：

1. **契约与扩展点**：M2-T1～M2-T3。
2. **MinerU 适配器与装配**：M2-T4～M2-T6。
3. **全链路与验收**：M2-T7～M2-T9。

若真实 MinerU 环境直到最后才可用，前两个 PR 不被阻塞；第三个 PR 负责真实集成出口。

## Risks and Mitigations

| 风险 | 影响 | 缓解 |
|---|---|---|
| MinerU 协议继续演进 | 高 | 校验 `protocol_version`；wire contract 集中在单个适配器并用快照响应测试 |
| 大 PDF 撑爆 HTTP 客户端或 Task JSONB | 高 | 流式上传；响应与 Markdown 独立上限；写 context 前复验 |
| 404 重提产生无限循环 | 高 | 单次提取最多重提一次，且共享总解析 deadline |
| PDF 长轮询占满 CPU lane | 中 | M2 先保持现有 lane；基准记录等待占用，达到瓶颈后单独设计动态分道 |
| 新单测突破 10 秒基线 | 中 | 轮询测试注入 sleeper/clock，禁止真实 sleep；每个 Checkpoint 复测耗时 |
| DOCX 行为被 Hook 重构影响 | 中 | 现有 DOCX 快照、四种 Pipeline 入口和 core-only CI 必须保持全绿 |

## Open Questions

- 真实 `mineru-api` / `mineru-router` 测试地址与可用时段。
- 三 PR 策略是否确认；若改为单 PR，提交边界仍按三个 Checkpoint 保留。
