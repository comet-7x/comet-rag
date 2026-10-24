# revision 历史恢复与迁移边界

迁移采用**新命名空间重建、核验后切换**。第二十一批交付默认关闭的
`RevisionIngestRunner`，复用准备/发布/清理协议；默认 IngestRunner 继续使用 legacy。
本批没有迁移用户资料、开启项目生产配置或切换流量。独立阶段和维护协议见[对账设计](../tasks/v2_reconciliation_design.md)。

## 历史任务按证据处置

自动对账只消费已持久化的准备意图或完整 handoff，不读取原文件来猜原计划。

| 记录情况 | 对账处理 | 边界 |
|---|---|---|
| 没有 revision 标记 | `ignored` | 不能因为 kind 相同就断言它没有旧向量；legacy 不属于精确 revision GC |
| 尚未终态 | `deferred` | 让原执行者完成或由已有租约流程恢复，不抢占业务 |
| 有有效准备意图或 handoff | 按 manifest、任务状态和发布回执决定 | FAILED 的 staging/ready 保留重试，CANCELLED 可回收；active 原子拒删 |
| 两种载荷都缺失或为 null | `missing_payload` | 保留源记录；不调用 reserve、abandon、向量删除或父块清理，不分配新 generation |
| 有非空载荷但格式、身份、指纹或版本损坏 | `failed` | 不退回猜测路径，不把损坏当作缺失或清理成功 |

`missing_payload` 只说明没有可验证的恢复 draft；并不保证残留的 key 完整、manifest 存在，
或其中的 revision 真属于这条任务。只有 key、原文、publication 回执或零计数，均不足以
恢复完整的原计划归属。尤其 `reserve_revision(draft)` 是可能创建记录的写原语，不能
把它伪装成只读探测。现有 Port 没有按 key 读取完整原 draft 的接口，本批不扩展它。

恢复选项只有有证据的两类：从一致性备份恢复原 Task/context 及配套 manifest/数据，
或保留旧记录并从已核验的原始资料在新命名空间重建。两者都需核对原任务、KB/source、
revision、完整 plan 指纹和 generation；不能手填一个猜测 draft 绕过校验。当前没有
“自动修复历史记录”的 HTTP/CLI，也未授权低层删 Task 或改 JSON 来消除告警。
没有可信资料的记录继续保留，不能以迁移其他文档成功代替其处置。

每页最多 32 个 `missing_payload` ID 随维护 result 持久化，`/admin/limits` 的
`revision_reconciliation.last_page.missing_payload` 可查询；旧 result 可能没有该字段，
读取方按空列表兼容。它与可正常延期的 `deferred` 分开，不意味着整页执行失败。
日志 `revision_reconciliation_missing_prepare` 标识具体源任务，
`revision_reconciliation_recovery_required` 对本页缺载荷发出错误信号。
result 仅保留最近一页，不是全历史或全轮审计；持续采集日志并留存处置清单，不能拿
下一页为空、`overdue=false` 或 `cycle_complete=true` 宣称历史问题全部消失。

## 暂停新入库，排干已受理工作

在所有 API 实例的配置中设置并重新部署：

```yaml
ingest_policy:
  accept_new_tasks: false
```

这个严格布尔开关默认 `true`。关闭后：

- `POST /ingest` 在来源 DNS/文件检查和创建 Task 前返回 HTTP 503。
- `POST /tasks/{id}/retry` 对 `ingest` / `revision-ingest` 同样返回 503，包括 `from_scratch=true`；
  不改原任务状态、输入、次数或续跑锚点。
- 查询、健康检查、取消和其他 kind 的控制仍可用。`/admin/limits.accept_new_ingest_tasks`
  显示当前进程的配置值，不代表全部署状态。
- 已受理任务、执行器自动重试、worker 租约恢复和队列消费继续运行，便于完成排干。
  该配置在 Context 构建时读取，不提供热切换管理端点。

这只是当前 API 准入，不是分布式写入围栏。配置更新前已经通过准入的请求仍可能创建
任务；必须等旧 API 实例和在途请求退出。库调用、直接 TaskService/Executor、外部脚本、
自定义 kind 及旧版本 API 不受它保护，应由部署方一并停投。不得用暂停开关、任务终态、
本地协程结束或“等几分钟”证明远端 RPC 以后不会提交。

排干顺序为：关闭所有生产入口 → 让队列/自动重试和在途生产者收尾 → 确认旧进程退出 →
保留旧数据与维护身份。新旧资源物理隔离后，无法证明最终停止的旧 RPC 仍只会写旧端；
旧命名空间不能随切换立即销毁，revision 补偿继续按原存储范围运行。

## 新命名空间的含义

最易核验的部署方案使用全新的 PostgreSQL 数据库、从未用于旧写的 Milvus database
或 collection_prefix，以及独立 Redis DB/队列，并保留旧部署配置。它们使用已有配置
字段，不引入新中间件。API 和所有新 worker 必须使用同一套新配置；不能只改 API。

只在同一存储内迁移部分 KB 时，至少采用从未使用过的新 `kb_id` 和新的 collection
前缀，显式划定来源 kind/维护范围、队列及 Task 存储归属；共享 PG 的隔离来自 KB 键，
不是两个 DocumentStore 连接。测试覆盖新 KB/前缀隔离，但没有把它当作跨数据库发布演练。
同一 TaskStore/source kind 的维护游标绑定原向量命名空间，改 prefix 后不能挪用旧游标。
新维护身份必须与新来源范围一致；旧游标保留并继续访问旧端，不能复制到新端伪装成进度。

以下都不构成新命名空间：删库后重建相同 KB、重启进程、同 collection 中换 revision ID、
或同 prefix 下统计为零。也不能向原 legacy 向量补 `document_revision` 就宣称完成迁移：
原模型、完整正文/结构、chunk 身份和父子关系需要重新校验。

## 生产 revision runner 配置

仅在核验过的新命名空间启用。下面片段需与新部署的 SQL、Milvus、Redis 和模型连接
设置合并；所有 API/CPU/IO 进程使用同一份配置、同一代码版本，不沿用旧端资源。

```yaml
backends:
  task_store: postgres
  task_executor: arq
  vector_store: milvus
retrieval:
  revision_aware: true
revision_reconciliation:
  enabled: true
  task_kind: revision-ingest
  page_size: 8
  timeout_seconds: 30.0
revision_ingestion:
  enabled: true
  kb_ids: [kb-new]
  parent_chunk_size: 8000
ingest_policy:
  accept_new_tasks: true
```

沿用已有配置创建新资源并应用现有三表迁移，然后分别启动 serve、preprocessor、embedder。
`COMET_RAG_CONFIG` 可统一指定配置路径；Alembic 也从该应用配置取 SQL 地址，不传入
其他 ini/DSN 覆盖。服务来源策略仍须单独开放并限制需要的 Local/URL/S3/PDF 能力。
建 KB 后再向 POST /ingest 提交相应 kb_id；不在 kb_ids 的请求在来源 IO 前拒绝。
CLI 的子块配置沿用 PipelineConfig 默认 2000/200；程序化注入 PipelineConfig 时，必须
给 API 和两道 worker 同时传入相同值，不存在额外的 YAML chunk_size 写开关。

- enabled 默认 false；开启必须配套 ARQ/PostgreSQL/Milvus、revision-aware 读和指定 kind
  的持续对账。kb_ids 为不重复、非空的显式清单，最多 32 个；父块大小不能小于子块。
- parent_chunk_size 非空时使用现有 HierarchyBuilder/RecursiveChunker，父块 overlap=0，
  保留 page/section 硬边界；null 时沿用已有 ChunkingService，生成 flat revision。
  层级模式不会调用任意自定义的全篇 document_chunker；仅接受 Builder 已支持的结构。
- 新模式只注册 revision-ingest，legacy writer 不同时注册；发现同一 TaskStore 已有任何
  legacy ingest 记录会拒绝启动。初版要求独立 Task 存储范围，不支持把已有旧任务表直接
  当作部分 KB 迁移环境。该检查不扫描外部数据，更不是从未用过存储的充分证明。
- 内部 request 的 revision_binding 是存储范围、模型名/维度、完整 Pipeline 设置、父块
  配置与 KB 清单的摘要，不含连接凭据。API 写入、消费者和手动重试共同复验，错配在
  外部写入前拒绝。它不是身份认证或跨存储事务；所有自定义生产者需遵守该协议。
  修改计划/批量/并发设置也会改变摘要，滚动改配置前须排干原任务；凭据轮换不改身份。
- 取源/解析共用 DocumentExtraction；CPU preparing 先保存计划身份，再写父块、保存
  handoff 和释放原文；IO 从 indexing 续跑，不能仅凭 ready 结束任务。
- 激活响应丢失保留同一 key/generation，已持久化发布后不再加载原文件/调用 embedding，
  包括人工 from_scratch。superseded 只清自身后成功结束，不覆盖更高 generation。
- 新 active 的 cleanup 依据 manifest.replaces_revision_id 回收前任；失败停在 cleanup
  续跑，不重新 stage/activate。发布可能已对查询可见而 Task 尚未成功，禁止据任务失败
  回滚 active。完成后持续对账仍重访旧源 Task，以补偿 GC 后迟到写。
- 默认整份 context 预算为 32 MiB，可由现有 limits 配置调整；提取、身份、移交、恢复与
  对账使用同一预算。扫描游标独立保留 4 KiB 上限，不用一个页的任务数量放大内存驻留。

网络/超时等已知瞬时错误沿用现有分类并受 max_attempts 限制，其他 SDK/契约错误可落
FAILED 后人工重试，不把未知错误全部伪装成瞬时故障。改模式或改绑定后 API retry 返回
409，暂停时两种入库 kind 均返回 503；其他 kind 的控制不受影响。历史坏记录不自动迁移。

保留新配置与 revision-aware 读取协议，不能在同一新存储上直接关闭 writer 配置后改跑
旧部署。旧二进制或已启动的旧 worker 不受新门禁保护，物理命名空间隔离仍是部署前提。

## 重建与切换验收

当前已经提供 opt-in 生产 runner，但资料清点、存储隔离、部署和切换仍由操作者执行。
下面是迁移验收要求，不是已经存在的一键迁移命令：

1. 固定资料清单、内容摘要、来源身份、模型名称/维度和分块配置；无法读取/确认的资料
   明确列为缺口，不从旧 ANN 命中结果推断完整文档。
2. 新端建立相应知识库，逐条按同一 revision 身份执行准备 → ready → 激活；只有子块
   embedding，父块落 DocumentStore。复用 RevisionTaskStages、StagePipeline 和现有
   CPU/IO 道次。按下文配置启用后，POST /ingest 直接选择 revision-ingest，无需另注册测试 kind。
3. 新端 revision-aware 读必须先启用并使用配套 DocumentStore/Strong；普通读路径遇到
   revision candidate 会安全拒绝。默认关闭写开关时 POST /ingest 仍写 legacy，不能用于重建新端。
4. 逐来源核对 active、父/子实际计数、内容与结构/查询样本；覆盖六种 dense/keyword/hybrid
   × child/parent 组合、KB/source 隔离和迟到旧写。空计数不是排干证据，数量一致也不是
   内容正确的充分条件。
5. 在目标部署复验跨道恢复、激活确认丢失、superseded 和部分 GC 失败；接通与新端
   匹配的持续对账和告警。对账能保留旧修订残留，不能清理无 revision 的 legacy。
6. 核对所有客户端路由与 API/worker 配置后再切读流量，随后才恢复新端入库；旧端维持
   停投和保留。迁移中发生失败时保持旧端服务或停在已知状态，不能混写两端凑数。

回退读流量之前，须明确新端在切换后是否接收过新资料。旧端若已落后，直接切回会丢失
可见更新；应先停止新端入库并保存差异，完成差异重建或使用已验收备份。保留 revision
读代码，不能让旧 M3 二进制读取已有 revision 数据，也不能直接 drop manifest 三表。

生产 H/I、实际资料迁移、部署切换/回退和完整旧端退出仍未完成；本文件给出实施范围与
验收条件，不声称本批已经迁移用户数据或完成 M4。
