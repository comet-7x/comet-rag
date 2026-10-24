# V2 后续重构计划

> 校准：2026-10-24；第十八批基于 `8c1ef8a` 继续实施。本文件维护下一步工作，历史证据留在
> [v2_plan.md](v2_plan.md)，M4 出口以 [m4_todo.md](m4_todo.md) 为准。

## 当前判断

前十六批已完成并分批提交：配置/资源生命周期、同步异步分离、原生 Milvus、DOCX 热点、
Filter 与 metadata 契约、重排边界、RunnerRegistry、查询 deadline、入库取消，以及
独立 revision 激活与 GC。第十六批完整验收为 unit 3064 passed、integration 245 passed/
2 skipped、E2E 50、benchmark 7；整体覆盖率 92.48%、tasks 95.95%。这些是既有记录，
不是第十八批运行结果；本批验证见实施记录。历史提交快照有超过 10 秒的单测记录，预算仍不稳定。

**V2 尚未完成生产闭环。** 第十七批在独立 RevisionTaskStages 中持久化发布意图、激活
结果和清理阶段，并提供 complete/purged 的精确补偿重放；已复现迟到写与普通扫描遗漏。
当前 `IngestRunner` 仍是 extracting → chunking → indexing，GC/补偿尚未注册生产
maintenance。revision-aware 读开关默认关闭，仍只有两种向量实现。

第十八批经授权最小扩展 TaskStore 游标和带守卫条件删除，交付持续对账页与应用保留
策略。已经复现 offset 饥饿和删除身份丢失，采用长期保留、至少一次重访；没有把本地
排干当成远端已停止。实际生产接线范围收窄，依据见 [对账设计](v2_reconciliation_design.md)。

依据：[入库 runner](../comet_rag/services/ingestion.py)、
[revision 阶段](../comet_rag/services/revision_context.py)、
[激活](../comet_rag/services/revision_activation.py)、
[GC](../comet_rag/services/revision_gc.py)。不能用独立切片通过代替 M4 Checkpoint H/I。

## 顺序与验收

| 批次建议 | 改造重点 | 完成条件 |
|---|---|---|
| 第十七批：独立恢复协议（已提交） | 已保存发布结果/GC 意图并复现迟到写，支持精确补偿 | 保留原故障回归；生产路径未切换 |
| 第十八批：生产前对账基础（本批） | 旧写保证、意图保留、删除约束和有界游标页 | 对账/守卫已实现；生产写不切换，缺载荷与 legacy 门禁仍在 |
| 第十九批：维护与前置缺口 | 扫描 Task 生命周期/所有权、触发/告警、缺 handoff 恢复与 legacy 迁移 | 保证资源命名空间一致、单进程不挂租约维护；先验收完整恢复身份，再确定生产写接线批次 |
| 第二十批：M4 出口 | 固定样本质量评测、安装/部署/回滚与文档验收 | 关闭已知验收缺口后，才讨论默认启用与发布 |

批次号表示建议顺序，不是工期或已批准的新接口。发现必须改变 schema、核心 Port 或
中间件时先提最小设计并按 spec 确认，不将其藏在“接线”里。

## 第十七批落实与生产前置条件

复用 `TaskContext.put_bounded`、既有 revision key/窄 handoff 和 StagePipeline。
优先在现有 revision 领域模块内定义少量明确状态，不创建第二套通用任务框架。
不增加 Task 表字段；context 仍受整份 32 MiB 上限约束，不存父块正文或模型向量。

本批的可执行恢复点（验证结果见实施记录）：

1. 激活已经提交，但任务结果保存失败：重试识别原 revision，不重新 embedding，
   generation/activated_at 不重复推进。
2. superseded 结果已保存，abandon 登记提交前失败：从登记阶段重试，不能当作 GC 成功。
3. abandon 已提交但响应丢失：独立实例能从候选恢复，任务重试不重新激活 abandoned。
4. purge 已提交但任务确认失败：重试认同墓碑，不能重新 stage 或分配更高 generation。
5. 激活后又被更高 generation 替换：恢复为 superseded，不能因旧结果持久化而回退 pointer。
6. 取消、租约丢失与进程关停发生在各边界：已发布版本保留，生产者先停、排干再关资源。
7. **迟到 upsert**：旧 RPC 在 GC 后才落库时，向量仍不可见，但可能成为不再进入普通
   候选扫描的残留。已注入并复现；保留 Task 意图并提供精确补偿入口。
   本地协程已结束不等于远端 RPC 未提交；不能直接宣称 Store 行锁解决跨库原子性。

第 7 项仍是生产启用前的硬门：现有原语不能证明任意旧 RPC 将来都不会落库，complete
仅表示上次核验为空。第十八批已选择持续对账、保留身份和游标重访；第十九批落实调度
及缺移交数据的恢复，不能仅凭独立页能力放开生产。空计数也不能证明 legacy 旧 RPC 已排干。
GC 候选扫描与具体任务结果仍分开；不将二者混为“失败整体重跑”的阶段。

验证至少覆盖：零网络故障单测、反向注入、真实 PostgreSQL/Milvus 的独立客户端恢复；
涉及跨道或执行器时加真实 Redis worker。检查默认 unit 耗时，并按 tests/README 合并
三层覆盖率。生产开关、GC 注册、既有快照保持不变，直到相应接线批次验收。

## 质量与出口工作

- 单测耗时单独审查冷/热运行和最慢分布。用事件协调替代不必要等待须有证据，不直接
  降低并发断言强度。不得把 instrumentation 耗时与普通 unit 预算混用。
- 建立可公开或可生成的固定文档/查询集：DOCX 快照、原生文本 PDF、扫描/混合页、
  中文多栏、表格、公式、损坏编码与超限输入。记录页/块身份、字符 span、表格完整性、
  Recall@k/MRR/nDCG、延迟分位数和峰值内存；同一语料、硬件、版本和配置才可比较。
- Ragas 先作独立离线评测候选。当前服务主要返回检索结果，先测检索质量；没有答案
  生成链路时不声称已验证 faithfulness。LLM judge 不替代人工标签和确定性回归。
- MinIO、外部模型/MinerU、真实部署及回滚尚未本轮补验；缺服务的 skip 不算通过。
  core-only 第十五批已隔离重装，第十六批复验冒烟；发布前再按目标安装方式验收。
- 完成 H/I 后校准 README、部署和迁移说明；新 revision 不默认开启前，需验证旧数据
  可见性和回滚流程。PR/CI/发布状态单独记录，不从本地测试推断。

## 生态候选不挤进主线

详见 [skills 与 RAG 生态复核](v2_ecosystem_review.md)。近期值得做的是独立样本对比：
pdf-inspector/LiteParse 的 PDF 预检或轻解析、anydoc 的多格式统一表示、Docling 的溯源
结构，以及 Ragas 的检索评测。先看真实质量收益，再提依赖和适配方案。

本阶段不整体迁移到 RAGFlow/LlamaIndex/Haystack，不替换已稳定的 DOCX 链，不安装
OCR/GPU 运行时进核心依赖。Qdrant/Weaviate/Chroma/OceanBase、Filter IR/新算子和新文档
后端需要另行定范围，不能因为上游有实现就自动扩大当前 spec A9/A13。
