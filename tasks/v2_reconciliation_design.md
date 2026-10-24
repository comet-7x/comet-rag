# V2：准备恢复与持续对账

第十八批经用户授权最小扩展 TaskStore：排他游标分页，以及带版本条件/保留守卫的删除。
第十九批复用这些能力和既有 DocumentStore/向量 Store 原语，补齐准备意图与维护接线。
不增加表、Task 字段、向量 schema、中间件或通用任务框架。验证记录见 [v2_plan.md](v2_plan.md)。

## 准备意图先于外部写入

`RevisionTaskStages.prepare` 的顺序为：

1. 校验原 revision key、完整计划和 context 预算。
2. 用 `put_bounded` 保存 `revision_prepare={version: 1, draft: ...}`，早于 reserve/父块写入。
   draft 包含 Task/revision 身份、指纹、计数和来源元数据，不含父块正文、child 正文或向量。
3. 用原计划幂等 reserve/写父块；失败保留输入和准备意图。
4. 父块写入完成后，一次 CAS 保存完整 handoff、清空准备意图并释放原文等重建输入。

准备意图确认丢失时，不继续外部写入；新 Context 重读原意图并复验同一计划。父块提交后
handoff 确认前后失败，可重放原计划或已提交的 handoff，不重新分配 revision/generation。
仅有准备意图时，`prepare(ctx, plan)` 仍需调用方从保留输入重建同一计划；它不猜测父块正文。
`prepare(ctx)` 无计划的快捷恢复只适用于完整 handoff，向量化仍先核对所有父块与完整指纹。

FAILED 的 staging/ready 保留业务重试，不能因一次失败就废弃；CANCELLED 不可重开，
对账可凭准备意图精确清理其部分父块。完整 handoff 与准备意图同时存在时必须一致。
第二十批把既无 handoff、也无准备意图的终态旧记录单列为 missing_payload，保留输入并记录明确告警；
这些旧记录需要经核验的迁移/恢复，不能事后猜指纹，也不宣称全部历史失败都已自动修复。

## 持续对账的保证

本地协程/线程/SDK 返回或取消，不能证明所有旧远端 RPC 已停止。采用至少一次、反复
精确对账，不引入“等 N 秒就已排干”的判断。complete/purged 仍重访；本次清理成功只
说明本次核验为空。active 由 Store 原子 abandon 拒绝保护，不凭调用方旧快照授权删除。

只有对账持续运行、身份保留、依赖最终恢复且旧写最终停止时，才能承诺最终清理；
没有某次空集后永久为空的保证。源任务不因对账改变状态、发布回执、输入或重试次数。

## 身份保留与删除

含 revision_key、revision_prepare、revision_index、revision_publication 的任务都保留，
包括 None/损坏、active、complete、终态和很久以前的任务；revision_scan 游标任务同样
保留。没有 TTL 或 force 例外。这需要存储保留成本，没有可靠替代账本不能先删 Task。

组合根给 TaskService 注入纯业务守卫，先拒绝已知保留项再考虑取消；实际删除以刚复验
的 version 原子执行，冲突重读并复验，显式版本冲突交给调用方。Task 内核不认识 RAG。
底层未传 guard 的 Store.delete 是通用/特权原语，不能用于应用 TTL 或例行维护清理。

## 有界页与恢复

offset 已复现持续插入下的饥饿。游标采用 `(created_at, task_id)` 排他倒序，PostgreSQL
用 C 排序与内存一致；锚点行删除、同时间戳不会打乱总序，新头部下轮再读。这不是快照，
不承诺无限回填旧时间时一轮必定结束，也不承诺数据增长超过处理速率时能及时完成全轮。

每次默认 8、最多 32 项，页工作预算最多 30 秒；逐条查询 limit=1，处理后才保存游标。
4 KiB 扫描 context 只保存版本、来源 kind 与锚点。任务 result 只覆盖最近一页的有界结果，
不是无限失败列表或完整审计账本；不把一页通过描述成全轮无故障。

- 历史缺载荷：本页 missing_payload 单列 ID，不猜测 reserve/GC、不阻塞后续项；处置需外部可信证据。
- 单项存储/校验失败：报告失败 ID，推进到下一项，下轮重访，避免坏头部阻塞所有旧记录。
- 查询、游标校验、预算或确认失败：整页异常，恢复已提交游标。
- 取消/租约丢失：停止后续 IO；GC 已提交状态保留，重放幂等。
- 到尾部：只重置游标并结束本次调用，不在一次触发中从头扫到空。

## 持久化维护任务生命周期

`RevisionMaintenance` 持有一个维护游标 Task，不进入业务执行队列；不改 Task 状态机。
它用既有 TaskStore CAS 认领/释放，再调用既有 `RevisionReconciler.run_page`。

| 事件 | 行为 |
|---|---|
| API/worker 启动 | 按来源 kind 幂等创建/加载同一个游标，校验存储命名空间摘要 |
| 一次触发 | PENDING → RUNNING，比较版本；分配唯一 invocation worker_id，attempts 单调增加 |
| 一页确认成功 | 保存最近一页 result，RUNNING → PENDING，释放 owner；下一次触发继续 |
| 整页失败 | FAILED + 稳定错误码；下一次定时触发显式 FAILED → PENDING 后重新认领，保留游标 |
| 单项失败 | 本页结果记录 failed ID 并打错误日志，下一页继续，下轮重访 |
| 进程退出/外部取消 | 不写假成功；现有 90 秒租约回收将失联维护任务判 FAILED，后续触发恢复 |
| 显式取消维护 Task | 落 CANCELLED，保留游标，不自动重建或复活；普通启停应使用配置而非 Task cancel |

`max_attempts=1` 让通用租约回收把失联维护 Task 判 FAILED，而不把它重投业务队列；
它不是维护轮次数上限。worker 的 PENDING 补投显式排除维护 kind，即使维护配置关闭也
不误投。SUCCEEDED 不用于页完成，FAILED 由维护触发恢复，不借业务重试队列执行。
应用准入和 backlog 按本 Context 当前注册的业务 kind 在 Store 查询时过滤，再应用上限，
不让内部维护游标挤占业务容量。TaskService 的 pending_kinds 是可选回调；直接使用通用
门面且未提供范围时，仍统计所有 PENDING，低层调用方自行明确其业务范围。

同一 TaskStore/来源 kind 只绑定一个向量 endpoint/database/prefix 摘要；切换命名空间
会拒绝启动，不能另建游标扫描同一批来源。摘要不包含凭据。不同 Context 不共享全局
对象；正确性依赖数据库 CAS 与 owner/attempts 围栏，cron unique 只减少重复触发。

启动后续装配失败时，已登记的游标保留，资源按现有回滚关闭；没有后台生产者需要偷跑。
停用配置需停止/排干旧 IO worker 后再关闭连接。重新启用复用 PENDING/FAILED 游标；
显式取消是终止该维护身份，需要经核验的维护迁移，不能用普通 retry 复活 CANCELLED。

## 调度与告警接线

自动维护默认关闭。启用必须有 ARQ/PostgreSQL/Milvus 与完整连接配置，并明确来源 kind；
组合根创建配套持久化存储，不接受不透明 Task/Document/VectorStore 注入来冒充持久化。
低层服务仍可显式注入内存实现验证契约，但不提供重启后的持久性。

仅 IO worker 挂 cron，每分钟第 23 秒、启动时也触发，每次最多一页；外层 45 秒预算，
已有租约回收在第 7 秒运行，租约 90 秒。IO worker 必须保留租约回收；CPU worker 不挂
revision 对账 cron。单进程 serve 不加载 workers.maintenance，也不启动隐式维护循环。
这不是生产 revision 写入开关；只启用维护也不会顺带启用 revision-aware 检索。

`GET /admin/limits` 增加 `revision_reconciliation`：任务 ID、状态、最近页、错误码和 overdue。
超过 180 秒没有确认一页即 overdue，初始以创建时间计；认领、心跳和重试不重置该进度时钟。
CANCELLED 显示已停状态，不因人为停用持续报 overdue。未开启维护时该字段为 null。

稳定日志事件用于现有日志采集/告警规则：

- `revision_reconciliation_failed`：整页失败；结合 error_code 与持续时间告警。
- `revision_reconciliation_partial_failure`：最近页有坏记录，结合该页 failed ID 与单项异常定位。
- `revision_reconciliation_recovery_required`：本页缺载荷，读取 last_page.missing_payload 并保留处置清单。
- `revision_reconciliation_missing_prepare`：历史缺载荷记录，需恢复/迁移，不能把普通 deferred 都当故障。
- `revision_reconciliation_retry` / `revision_reconciliation_page`：恢复与进展记录。

监控应同时检查错误、部分失败和 overdue；只有 RUNNING/心跳不代表维护有效。
overdue 只衡量页进度，不保证全轮及时完成；应另外从 page 日志的 cycle_complete=True
统计轮次耗时，并按数据规模设阈值，不能把每页有进展当成全库补偿 SLA。仓库交付
这些可查询/可采集的信号与规则，不新增告警中间件或对外发送通知。没有 IO worker、
Redis 调度停止等“无人执行便无日志”的故障，应通过外部轮询 overdue 发现。

## 生产写入仍需验收

本批接入默认关闭的独立维护，不切换 IngestRunner，不默认开启 revision 读写，不勾选
完整 H/I。生产新写前仍需处理旧缺载荷记录、legacy 退出与命名空间/迁移验收，并完成
准备→激活→清理的生产整体接线、部署和回滚验收。

旧 flat 写的无 revision 数据不属于精确 GC 过滤；空集合不是在途旧 RPC 排干证明，再建
同名 KB 也不是新命名空间。限定从未用于旧写的 KB/存储命名空间，或经过明确验收的
迁移批次。不能把有独立维护入口或 cron 存在，推定整个生产写路径已安全。

## 第二十批：迁移处置范围

历史终态缺载荷与普通延期分离，但没有补造原计划或自动修复历史记录。用户选择
新命名空间重建后切换；API 准入暂停只控制新提交和手动 ingest 重试，已受理工作继续
排干。部署范围、数据证据、命名空间、生产接线与回退门禁统一见
[历史恢复与迁移边界](../docs/revision_migration.md)。当前生产 runner 仍是 legacy。


## 第二十一批：生产 writer 配对

revision_ingestion 明确开启后，组合根注册 revision-ingest 并要求相同来源的维护和
revision-aware 读取；默认 legacy 路径不变。新 active 的 cleanup 同步精确回收 manifest
记录的前任，失败续跑 cleanup；持续对账仍按旧源 Task 身份重访，不能据一次 GC 完成
删除旧意图。writer/恢复/对账共用配置的源 context 预算，游标仍是独立 4 KiB。
新路径已接线，实际用户资料迁移和全环境切换/回退仍未验收，详见
[生产配置](../docs/revision_migration.md#生产-revision-runner-配置)。
