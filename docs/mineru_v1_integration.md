# MinerU V1 显式接线与恢复约定

2026-10-28。V1 来源锁、持久化许可、发布编排和授权证据读取已接入可选组合根。
**默认仍走既有协议；没有修改生产配置。** 新路径仅适用于预检通过的新 revision
命名空间，不是旧 Extractor 的透明替换。完整结果与遗留门禁见
[收口验收](evaluations/mineru_wiring_20261028.md)。

## 接线入口和资源所有权

`build_context(..., mineru_factory=factory)`、`create_app(..., mineru_factory=factory)`
及 `workers.base.build_settings(..., mineru_factory=factory)` 使用同一个异步工厂口。
当前没有新增 YAML/CLI 自动启用开关；直接运行原有命令不会启用 V1。

工厂签名在 `composition/mineru.py`：接收当前配置、TaskStore、DocumentStore 和
AsyncExitStack，返回 `MinerUIntegration(workflow, reader)`。模型和资源仍由组合根
选择，业务服务不 import SDK。工厂须在当前事件循环创建自己的 HTTP/S3 客户端，
创建后立即登记 `resources.enter_async_context(...)` 或 `push_async_callback(...)`；
先登记下游连接、后登记依赖连接的生产者，使栈逆序关闭。不要在模块导入时创建
AsyncClient，也不要让两个 Context 共管同一个客户端。

启动失败会等待所有已登记资源回滚，关闭错误记录后继续清理并保留原始启动异常；
启动成功后 Context 接管该栈。正常关闭先停执行器并排干业务，再撤销注册、关闭工厂
资源，最后关闭存储和数据库。并发关闭共用一次清理，重复取消也要等待排干。
直接注入 workflow/reader 仍可用于外部托管对象，但不与 factory 混用；其客户端
生命周期归注入方，不能误以为 Context 会发现并关闭 workflow 内的借用连接。

启用前必须满足 revision 写入、父块配置、KB 白名单和配套存储约束；新旧 MinerU
协议互斥。发现 legacy ingest 或 flat 写围栏记录时拒绝复用任务命名空间。进程间绑定
包含 revision 配置及 MinerU 端点/S3 目标摘要与存储别名，不能漂移后继续旧任务。

## 来源许可

`infrastructure.evidence.source_locks.load_source_locks(path)` 读取最多 1MiB 的精确
JSON：顶层为 `{"version":1,"sources":[...]}`，最多 1000 条且不允许重复 KB/来源身份。
每条必须明确提供 `kb_id`、`source_id`、`source_sha256`、`size`、`pages`、`filename`、
`approval_id`。来源 ID 使用现有 SourceContent 规则，SHA 对应实际 PDF 字节；物理页数
必须来自独立预检，不能用 MinerU 返回的 full 标志或请求 metadata 自证完整。

文件大小最多 100MiB、页数 1..512；整型不接受 bool，摘要和文件名严格校验。真实
取源快照再次核验 SHA/长度，改名或同长度改字节都不能获准提交。批准目录属于服务端
配置，不接受 HTTP 调用方自带来源锁。approval_id 是受控预检身份，不是数字签名，
也不等于未参与出题者的独立质量复核。

## 发布许可和业务检查点

组合根在准入前调用 workflow.prepare_publication，复用 TaskStore 的唯一幂等键和
CAS 保存 `evidence-publication-permission` 控制记录。记录不入队，PENDING 封存到
CANCELLED；该终态只是“非执行任务”，不代表远端探测被取消。

| 顺序 | 持久化事实 | 后续副作用/恢复 |
|---|---|---|
| 创建 | 存储别名、目标摘要、原生 SDK 唯一 probe_key | 尚未做条件写 |
| CAS 领取 | probing 和本次 invocation | 才调用原生 verify_target |
| 原生验收 | 缺失可创建、已有拒绝覆盖、checksum、有界回读 | 仍不能只凭成功回包清意图 |
| CAS 确认 | verified | 当前 SDK 实时许可也有效时才完成启动 |

目标验收总 deadline 最多 120 秒。取消、超时、PUT 或 CAS 响应丢失保留精确探测身份；
同一 probe 不自动重试。确认 CAS 回包丢失后可只读确认；历史 verified 不能授权一个
未验收的新 SDK。新实例使用新 probe，旧不确定记录与对象仍保留，不推断旧 RPC 已排干。
记录默认不可 force/TTL 删除，通用 PENDING 补投排除该 kind。此处不新增后台扫描、
探测对象 GC 或无限期容量承诺；部署方须监控记录/对象增长并制定保留及归档策略。

业务 Task 中只存小引用和固定只增标记：

| 标记 | 事实与恢复约束 |
|---|---|
| mineru_capture_lock | 来源、revision 与端点/存储绑定 |
| mineru_capture_submit | POST 前授权已持久化；无远端 ID 时不自动重提 |
| mineru_capture_remote | 取得安全 task_id；尚不能证明来源属于本任务 |
| mineru_capture_source | 原生 SHA、大小、名称和用途已经核验 |
| mineru_capture_publish | 任何业务对象 PUT 前保存精确 manifest 引用 |
| mineru_capture_committed | 完整回读核验成功后确认同一引用 |

标记总量最多 8KiB，并与既有任务 payload 共用整份 context 上限。已有任意标记的
记录均受删除守卫保护，损坏和历史实验标记也保留。旧 attempt 围栏、前置检查和预算
都在 CAS 内执行。POST 与 SQL 没有跨系统事务，不宣称 exactly-once。

先尝试按精确引用回读：已提交包无需源临时文件或远端任务继续存活。只有真正缺对象
才允许对已知原任务恢复产物；403、错摘要、损坏、超时不能伪装成缺失。恢复产物
必须等于原发布意图，不能换一个 manifest。已知任务可能已失败或被原生取消，恢复
不会保证它最终完成；需要运维核对，而不是重新 POST。

## 统一资源预算和证据表示

取源、闸门排队、采集、发布共用最多 900 秒，包含原生取消收尾预留；不在每阶段
重置 deadline。取消时等待本地文件操作/临时目录清理，原生 DELETE 响应不等于远端
资源排干。HTTP 原生 JSON 最多 32MiB、累计接收 36MiB，S3 各文件和累计回读仍遵守
原有协议上限，不把内嵌图片放进 Task、父块或向量 metadata。

复用原生 structured_content 生成 canonical、页事实和证据账本。图像解读与提取
正文分离；父块只保存受保护的小引用，子向量不携带证据引用。正式 runner 复用
preparing/indexing/activating/cleanup 阶段和已有 Store 原语。生产 page 分块策略及
页内标题视图未因此切换，跨页标题与长表质量仍需独立对照。

## 授权读取

显式配置 EvidenceReader 后提供 `GET /evidence/{kb_id}/parents/{parent_id}`。
缺 reader 返回 404；缺 Bearer 返回 401；token 错误或 KB 越权返回 403。
服务端仅保存 token SHA256 和批准的 KB 集合，不能把来源 SHA/父块 ID 当凭据。
授权检查先于 DocumentStore/S3 IO。返回带 no-store，领域异常统一脱敏。

只读取当前 active 父块：核对 KB、revision、引用和 canonical 原文区间；完整核验
原生包，S3 读取后再次判活。单次读取默认 30 秒、最多 120 秒；父块最多 256KiB、
512 个证据片段，标题路径/JSON 响应预算有限。只返回选定父块的原文与片段证据，
不返回 PDF、内嵌资产、整个账本或存储地址。激活指针两次检查是快照约束，并非跨
存储线性一致性事务。异常不回退未核验正文。

此鉴权只保护新证据入口。其他参考 API 仍依赖可信网络或外部网关；没有据此实现
全局身份系统。生产公网接入前必须单独验收统一认证/授权。

## 不确定状态的处置

停止新写入并保留 Task/控制记录，按固定目标、probe_key、远端任务 ID 和 manifest
引用核对；不要年龄到期就删、不按 SHA 猜任务、不扫描并删除整个 bucket/upload 目录。
确认数据完整、远端写已排干并独立归档审计身份之后，才能另行设计解除守卫/对象 GC。
本次没有新增管理删除后门、自动补偿调度或对象 TTL。

目标旧 MinIO 的条件写门禁仍拒绝。SeaweedFS4.51 单机单 filer/双 gateway 验收不替代
实际部署的多机、断电、TLS/IAM、容量、备份及切换/回退验收。独立标签、不可变模型
工件、MinerU 关停 semaphore 警告与严格 10 秒单测门禁继续保留。
