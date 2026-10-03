# 企业级 RAG 后端：可靠性重构

> 基线：`origin/develop`，`75d1248`（M3 与仓库结构归一化）
> 分支：`refactor/enterprise-reliability`
> 日期：2026-10-02

保留库与参考服务的双重定位。先保证长任务、故障恢复和文档更新的行为可验证，
再推进企业接入、真实检索评测与开源发布。

## 首批实现

- [x] 执行器在整个 runner 生命周期内持续续租，长阶段无需额外 checkpoint。
- [x] 续租在每轮 CAS 中校验 worker、尝试次数与活跃状态，保留心跳不涨版本的契约。
- [x] 失去租约或续租失败先取消并排干 runner；执行结束后排干续租协程。
- [x] 拒绝 ARQ 与内存任务/向量存储组合，保留单进程内存部署。
- [x] CI 增加内存端到端测试；核心安装检查使用 `uv run --no-sync`。
- [x] 新增跨实现续租测试与长阶段、接管、失败回归测试。

首批不修改 Task 持久化字段、抽象存储原语、向量 schema 或依赖分组。
取消 runner 协程不能撤回已发生的外部写入，也不能强制结束已启动的线程计算；
后续文档版本提交仍需独立围栏。

### 验证记录

- 单元：1874 passed、20 skipped、1 xfailed；整体覆盖率 85.31%。
- 内存端到端：30 passed。
- Ruff、Pyright、`git diff --check` 通过。
- 反向注入停用续租、删除所有权检查和停用配置校验后，5 个目标用例失败；
  恢复实现后完整测试通过。
- PostgreSQL TaskStore 契约：本地中间件不可用，56 个用例 skip，尚未验证真实后端。

## 第二批：可靠投递

- [x] 设计数据库建任务、初次投递、重试和 worker 移交的故障矩阵。
- [x] 选择有界对账，明确重复投递语义与恢复时限。
- [x] 保留业务重试退避，不因补投让延迟任务提前执行。
- [x] 单元故障注入覆盖 Redis 故障、API/worker 在投递前退出，以及回收后再次投递失败。
- [x] 在独立测试 Redis/PostgreSQL 上执行新增恢复测试与原有跨进程契约。

在 worker 的租约回收之后补做 PENDING 对账，每轮最多 100 条，分页游标保留在
执行器实例中并轮转。此方案不改变 Task 持久化字段或核心抽象接口，无新增迁移。
Redis job ID 以现有任务版本区分投递批次，重复消息仍由 Redis 去重与认领 CAS
共同保护；只承诺可恢复的至少一次投递，不宣称外部写入恰好一次。

| 故障窗口 | 库中状态 | 恢复动作 |
|---|---|---|
| 建记录后 API 退出 / 首次 enqueue 失败 | PENDING | 对账补投入口道 |
| 可重试失败落库后 worker 退出 / enqueue 失败 | PENDING + error | 按 error.at 与 attempts 恢复绝对退避时间 |
| 移交落库后 enqueue 失败 | PENDING + resume_stage | 按 runner.lane_of 投到目标道，不消耗重试次数 |
| 租约回收后 enqueue 再次失败 | PENDING + lease_expired | 后续对账立即补投，不附加业务退避 |
| ARQ 在认领前遇到数据库故障并留下 result key | PENDING | 移除该版本的传输结果键，再投递 |
| 人工重置次数 / 旧延迟消息到达 | 新一轮 PENDING | 新版本 job ID；旧消息不能认领新版本 |

恢复时限是“下一次成功扫描到该记录”，受积压、分页、副本更替和执行槽影响，
不是固定 SLA。单轮有界扫描不解决任意持续 churn 下的严格公平性，也不提供
事务 outbox 的提交原子性；如需硬恢复时限，再按 spec §7 评审持久化调度/outbox。

业务退避起点来自现有 TaskError.at，要求所有进程的 retry_backoff 一致。
`submit(delay=...)` 的额外自定义延迟未持久化，不纳入丢失 Redis 后的恢复保证。
补投不修改任务版本、context 或 attempts；过期多日的退避改为立即投递，避免
ARQ 根据过去的截止时间算出负 job TTL。入库 runner 暴露已有流水线的 lane_of，
修复 indexing 恢复时可能回落到错误 worker 道的问题。

第二批验证记录：反向注入停用对账、丢弃退避、删除投递版本和两处旧消息守卫，
6 个目标用例均失败，已恢复实现。

- 单元：1888 passed、20 skipped、1 xfailed，9.28s。
- 内存端到端：30 passed，1.39s。
- Ruff、Pyright、`git diff --check` 通过。
- ARQ / PostgreSQL / 崩溃恢复集成：83 skipped，中间件未运行，不能算验证通过。

以上为第二批结束时的历史记录；真实后端验证已在下面的第三批补齐。

## 第三批：收尾围栏与真实后端验收

- [x] 状态迁移、context、进度与阶段写入的所有权守卫放入每轮 CAS。
- [x] 收尾绑定尝试次数，防止同 worker 的旧尝试在下次心跳前覆盖新尝试。
- [x] 本地认领令牌区分移交退还次数后的再次认领；旧执行者退出不摘掉新尝试。
- [x] 取消与认领/完成冲突后有界重读，重新选择迁移目标。
- [x] 关停只取消本实例拥有的尝试，不取消接管者。
- [x] 租约回收在实际更新时复查心跳、状态和所属尝试，跳过已续租的旧候选。
- [x] context 合并在 CAS 内完成，冲突重读保留并发写入的其他键。
- [x] 测试探活与崩溃子进程使用实际 COMET_TEST_* 端点和显式临时配置。
- [x] CI 增加 PostgreSQL/Redis 任务测试，服务不可用不得 skip，并合并三层覆盖率。

本批没有新增持久化字段、迁移、依赖或核心抽象方法；guarded 模板为 TaskStore
内部实现，原有公开方法与抽象后端原语的签名保持不变。新围栏保护任务状态与
中间态，不代表已经完成向量写入的 revision / 激活闭环。

验证使用独立 Docker 项目 `comet-rag-reliability-test-nmcyi2`，端口
15432 / 16379，数据库 `comet_rag_reliability_test`，无开发数据目录挂载。
只向该临时库应用 develop 已有的两条迁移，未操作开发数据库。

初始缺陷验证：10 个所有权/心跳竞态用例与 2 个取消竞态用例在修复前全部失败，
修复后通过；TaskStore 新增三条共享契约同时由内存与 PostgreSQL 验收。
再次注入停用状态围栏、破坏 context 合并、忽略测试端点、放宽 CI 服务要求及
无条件摘除本地认领记录，13 个目标用例失败，恢复后完整验收通过。

最终验证：单元 1914 passed / 20 skipped / 1 xfailed，独立运行 9.25s；内存端到端
30 passed；真实 Redis/PostgreSQL 与 SIGKILL 接管 86 passed，无 skip。
单会话合并验收 2030 passed，23.30s；合并整体覆盖率 88.74%，任务框架
95.51%（830 / 869 statements），满足 S5。Ruff、Pyright、工作流 YAML 检查及
`git diff --check` 通过。CI 已配置，尚未推送触发远端执行。

临时项目已通过 `docker compose ... down --volumes` 清理：两项测试容器、
项目网络和匿名测试卷不再保留，测试数据可重跑生成；开发数据未改动。

## 文档版本闭环

- [x] 与保留的 M4 分支对齐 revision / DocumentStore 设计与实现。
- [x] 合入完整 M4 已有提交，并恢复原来两处 Chunker 修改。
- [x] T8 第一切片：HierarchyBuilder、IndexPlanner 与仅准备到 ready 的 RevisionStager。
- [x] T9 读侧切片：有界 revision 判活、RRF 前过滤、父块批量回填与故障诊断。
- [x] 配置化 DocumentStore/默认关闭的读开关与启动前资源配对校验。
- [ ] 真实 Milvus 双客户端 Strong 验收（MinIO 镜像来源不可用，未改用第三方镜像）。
- [ ] T8 后续：有界 Task context、跨 worker 续跑、激活/响应丢失与 superseded 收尾。
- [ ] 写入、可见性判定、激活和旧版本回收一起验收。
- [ ] 覆盖窗口写入失败、同源并发更新、过期执行者提交及回滚。

用户确认后，本分支已从 `75d1248` 快进到 M4 分支头 `20670c1`，完整保留 T1～T7
提交与三表迁移。此前可靠性改动经 stash `10462ddf` 保存并恢复；原始两处改动经
stash `9f56e817` 恢复。这两份备份仍保留，合并冲突已解决，未推送。

T8 第一切片把 page/section 硬边界内的父子块物化为 revision 隔离的 ID 与计划指纹，
父块分批写入、子块窗口化 embedding/upsert、精确 count 后标 ready。中断保持 staging
供同 revision 重放；内容漂移、计数不符和已关闭 revision 均拒绝。准备服务不激活，
也未接入组合根；生产 IngestRunner 仍是原链路，不能据此宣称文档更新闭环已修复。
按 T6 发布顺序，必须先验收 revision-aware 读路径，才允许启用新写入与激活。

故障测试覆盖父块/向量提交后响应丢失、embedding 失败、计数过少/过多和 ready 响应
丢失，并验证旧 active 指针与旧向量未被修改。新规划与准备路径有 29 项单测；反向
移除计数、父块重叠、指纹和相邻关系防伪守卫，5 项目标测试失败，恢复后通过。隔离核心安装可完成
DOCX 解析、层级分块和 IndexPlanner，且没有安装 sqlalchemy/redis/pymilvus/fastapi/arq。

真实测试沿用独立 Docker 项目与 15432/16379 端口，空测试库已升级到 M4 migration
`6c31f4e2a9d7`；开发数据库与原 collection 均未操作。

最终单元验收：2156 passed / 20 skipped / 1 xfailed，独立运行 9.32s；内存 E2E
30 passed；真实 PostgreSQL/Redis 107 passed（包括 19 条文档契约和 2 条新写入组合测试），
无 skip。三层合并验收 2293 passed，25.53s；整体覆盖率 89.55%，任务框架 95.63%。
Ruff、Pyright、工作流 YAML 与 diff 检查通过，CI 已增加文档契约与核心层级 smoke，
尚未推送执行。Milvus 双 client/Strong、新读写切换和完整故障矩阵尚未验收。

隔离测试容器、网络与匿名测试卷已清理，测试数据可重跑生成；开发资源未改动。

## T9 读侧切片验收

2026-10-03，T9 读侧切片已落地。每通道最多两轮 / 2048 候选，DocumentStore 查询
失败或映射不完整拒绝整次检索，不能降级泄漏旧 revision。parent 模式对子块完成
融合/重排后遍历全候选去重，再批量回填并截 top-k；错配/缺失回退 child 并诊断。
API 新增 return_level 与 matched_child_id（默认 child 时为 null）。Milvus 查询/count
显式传一致性级别；组合根仅在显式注入 DocumentStore 时创建 Strong 适配器。
CLI 和生产 IngestRunner 尚未切换，不宣称新写入故障闭环已完成。

独立单测 2208 passed / 20 skipped / 1 xfailed，最终复跑 9.28s；内存 E2E 30 passed；
真实 PostgreSQL/Redis 108 passed，无 skip。三层合并 2346 passed，25.74s，整体
覆盖率 89.81%、任务框架 95.51%。5 类反向缺陷被 9 项目标测试抓住；Ruff、
Pyright、diff 检查通过。Milvus 双客户端用例已添加，但服务未运行而 skip。
隔离测试资源清理后不保留测试数据。T9 切片按读侧核心、服务接线/验收与文档
三批本地提交，未推送。

## 配置化读侧装配验收

既有 T9 改动已按三批本地提交：`81fb9a3`、`87f8982`、`6e809fa`。三份暂存快照
单独通过 2205/2208/2208 项单测（9.78/9.48/9.45s），commitizen 通过，未推送；
临时导出快照已删除，代码可从提交重新生成，原始 stash/branch bundle 备份未删除。

继续实现默认关闭的 `retrieval.revision_aware`，严格候选预算与资源配对。组合根
自动装配 DocumentStore，把自行注入的 Session Milvus 在创建其他资源前拒绝，
并持有仅因 DocumentStore 而创建的 SQL pool；既有入库/检索 E2E 两种读模式均通过。
独立 unit 2233 passed / 20 skipped / 1 xfailed，9.14s；内存 E2E 49 passed；
真实 PostgreSQL/Redis 109 passed。三层合并 2391 passed，26.24s；整体覆盖率
89.94%、任务框架 95.51%。5 类反向缺陷被 6 项目标测试抓住；静态/类型/diff 与
隔离核心安装检查通过。配置与装配分别本地提交 `2891f5c`、`b456653`，未推送。
配置暂存快照 2224 项单测通过，9.98s；装配快照 unit/E2E 合计 2282 passed，11.05s。

隔离 Milvus 实测在依赖镜像处受阻：MinIO Docker Hub denied，官方 Quay 401。
未改开发 compose，也未使用第三方镜像/新中间件。隔离测试资源已清理，测试数据
可重建；拉取的 Milvus/etcd 镜像留作缓存。需可访问的 MinIO 镜像或隔离 Milvus 地址
后再验收 Strong，随后推进 T8 Task context/跨 worker 续跑、激活/superseded 和 GC。
生产读开关仍未开启，新写入仍未接入。

## 后续产品范围

- [ ] 定义身份、知识库与文档授权边界。
- [ ] 建立固定语料、相关性标注与真实检索评测。
- [ ] 完成 readiness、跨进程追踪、运行指标和备份恢复流程。
- [ ] 确定许可证、贡献指南、兼容政策与可复现发布方式。
