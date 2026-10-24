# revision 历史恢复与迁移边界

第二十批确定采用**新命名空间重建、核验后切换**。当前生产 `IngestRunner` 仍写 legacy
平坦块；本批提供历史缺载荷诊断和 API 入库准入暂停，未执行数据迁移、实现新的生产
revision runner 或切换流量。独立阶段和维护协议见[对账设计](../tasks/v2_reconciliation_design.md)。

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
- `POST /tasks/{id}/retry` 对当前生产 `kind="ingest"` 同样返回 503，包括 `from_scratch=true`；
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

## 重建与切换验收

当前可执行准备仅限准入暂停、迁移资料清点、隔离存储和独立阶段验证。下面的生产步骤
是后续接线的验收要求，不是已经存在的一键命令：

1. 固定资料清单、内容摘要、来源身份、模型名称/维度和分块配置；无法读取/确认的资料
   明确列为缺口，不从旧 ANN 命中结果推断完整文档。
2. 新端建立相应知识库，逐条按同一 revision 身份执行准备 → ready → 激活；只有子块
   embedding，父块落 DocumentStore。复用 RevisionTaskStages、StagePipeline 和现有
   CPU/IO 道次，生产注册和入口开关另作一批实现，不新增泛化工作流。
3. 新端 revision-aware 读必须先启用并使用配套 DocumentStore/Strong；普通读路径遇到
   revision candidate 会安全拒绝。**不能把当前 POST /ingest 当作第 2 步**，它仍写 legacy。
4. 逐来源核对 active、父/子实际计数、内容与结构/查询样本；覆盖六种 dense/keyword/hybrid
   × child/parent 组合、KB/source 隔离和迟到旧写。空计数不是排干证据，数量一致也不是
   内容正确的充分条件。
5. 对新生产 runner 验证跨道恢复、激活确认丢失、superseded 和部分 GC 失败；接通与新端
   匹配的持续对账和告警。对账能保留旧修订残留，不能清理无 revision 的 legacy。
6. 核对所有客户端路由与 API/worker 配置后再切读流量，随后才恢复新端入库；旧端维持
   停投和保留。迁移中发生失败时保持旧端服务或停在已知状态，不能混写两端凑数。

回退读流量之前，须明确新端在切换后是否接收过新资料。旧端若已落后，直接切回会丢失
可见更新；应先停止新端入库并保存差异，完成差异重建或使用已验收备份。保留 revision
读代码，不能让旧 M3 二进制读取已有 revision 数据，也不能直接 drop manifest 三表。

生产 H/I、实际资料迁移、部署切换/回退和完整旧端退出仍未完成；本文件给出实施范围与
验收条件，不声称本批已经迁移用户数据或完成 M4。
