# MinerU 证据存储的持续维护与部署方案

2026-10-28进展：SeaweedFS4.51完成单filer／双gateway协议和进程恢复验证，正式
采集／存储模块及父块保护已实现；未批准真实多机拓扑或切换。迟到探测写及门禁见
[本批报告](evaluations/seaweed_evidence_20261028.md)。以下保留原选型背景及待执行方案。

2026-10-27。**建议采用独立 S3 证据端点，开源路线先验收 SeaweedFS 4.51，RustFS 1.0.4
作为备选。** 这是选型及待执行的部署方案，不表示新后端已通过项目验收，也没有安装
新中间件、修改 compose 或切换生产配置。用户已确认优先自托管开源方案；商业及
托管路线保留为对照，不进入本轮实施主线。
已核实的目标 MinIO 缺口见[诊断报告](evaluations/minio_diagnosis_20261027.md)，版本
快照见[选型记录](evaluations/object_storage_selection_20261027.json)。

## 选择依据

本项目需要的不是泛泛的“S3 兼容”，而是固定制品在实际拓扑上同时满足：缺失对象
条件创建、已有对象拒绝覆盖、并发唯一写入、读取故障时拒绝写、提交不确定后显式
重试、SHA256 checksum、有界回读，以及可演练的备份恢复。普通上传下载通过不够。
维护活跃、正式 release、许可证和支持渠道也不能代替这些行为验收。

| 路线 | 当前官方证据 | 对项目的决定 |
|---|---|---|
| MinIO 社区原仓库／本机固定源码候选 | 官方声明不再维护；上轮候选仅通过限定功能测试 | 不作为持续维护方案；不由 Comet-RAG 接管一个存储内核分叉 |
| SeaweedFS 4.51 | 2026-10-25 正式发布；原生条件写与上游回归存在，Master／Volume／Filer 分工明确 | 首个隔离验收对象；需验证 filer 元数据、路由、锁与故障切换，不能只测单 gateway |
| RustFS 1.0.4 | 2026-10-25 正式发布，非 prerelease；已有条件写及分布式故障／升级测试 | 备选；不沿用早期 alpha 评价，也不将正式标签等同于项目生产达标 |
| MinIO AIStor | 官方在维护的产品路线；Free 限单计算资源且不含支持 | 接受许可时的备选；高可用与支持按实际订阅范围核实，不能把 Free 当免费分布式版本 |
| Ceph RGW | 官方 Tentacle 20.2.4 分支有维护周期 | 已有 Ceph 团队／集群时复用；不为当前一条证据链先新建完整 Ceph 集群 |
| Garage | 官方 known-issues 明确说明缺少安全并发条件写 | 不满足当前不可变发布协议，排除；不靠应用锁补偿其存储语义 |
| 托管 AWS S3 | 官方提供原生条件写与策略约束 | 接受托管时可选；仍需核实区域、网络、权限、费用与项目实际验收 |

来源：[MinIO 维护说明](https://github.com/minio/minio/blob/7aac2a2c5b7c882e68c1ce017d8256be2feea27f/README.md)、
[SeaweedFS 4.51](https://github.com/seaweedfs/seaweedfs/releases/tag/4.51)、
[RustFS 1.0.4](https://github.com/rustfs/rustfs/releases/tag/1.0.4)、
[AIStor 安装与 Free 限制](https://docs.min.io/aistor/installation/macos/install/)、
[Ceph 维护周期](https://docs.ceph.com/en/latest/releases/)、
[Garage 限制](https://github.com/deuxfleurs-org/garage/blob/586726edb0170bb46f604e84fba09e5659610649/doc/book/reference-manual/known-issues.md)、
[AWS 条件写](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)。
Ceph Squid 的官方预计 EOL 为 2026-10-31，不为新部署推荐临近结束维护的分支。
上述版本与维护状态是本日快照，部署前必须重新确认；活跃社区项目不等于附带商业 SLA。

## 固定到可复核的候选

SeaweedFS 首个验收基线为 4.51，commit
`153ab668c99731a6d29492bf6c61d0b4fa701101`。固定源码已检查：

- [条件头映射](https://github.com/seaweedfs/seaweedfs/blob/153ab668c99731a6d29492bf6c61d0b4fa701101/weed/s3api/s3api_object_routed_write.go)
  将 `If-None-Match: *` 降为原生 IF_NOT_EXISTS；无法归约的条件保留原生锁路径。
- [PUT 实现](https://github.com/seaweedfs/seaweedfs/blob/153ab668c99731a6d29492bf6c61d0b4fa701101/weed/s3api/s3api_object_handlers_put.go)
  包含 routed 写、元数据条件与锁路径；条件写遇到 owner 不确定有专门处置。它不是
  一次 HEAD 加一次普通 PUT，但路由变化／锁失效是否满足本项目契约仍须实测。
- [上游回归](https://github.com/seaweedfs/seaweedfs/blob/153ab668c99731a6d29492bf6c61d0b4fa701101/weed/s3api/s3api_object_handlers_conditional_write_test.go)
  明确覆盖缺失／已有对象及通配条件；本轮只读测试源码，没有执行 SeaweedFS 服务或其测试。

RustFS 备选固定 1.0.4，commit `46337942d55f3a8e6a97b147ca98d9ea8115555d`；已检查
[原生条件写用例](https://github.com/rustfs/rustfs/blob/46337942d55f3a8e6a97b147ca98d9ea8115555d/crates/e2e_test/src/reliant/conditional_writes.rs)
与[分布式验证范围](https://github.com/rustfs/rustfs/blob/46337942d55f3a8e6a97b147ca98d9ea8115555d/docs/testing/distributed-e2e.md)。
不据“用 Rust 重写”推断数据可靠性，也不直接复用 MinIO 的物理数据目录。

## 部署范围与职责

1. **独立证据端点／新 bucket／新数据目录**。原始文档来源、证据包、Milvus 内部对象
   数据分别界定所有者。仓库 compose 的 Milvus 指向 minio 服务；不能为更新证据
   存储就替换这个共享服务或全局改 MINIO 地址。远端实际共享关系本轮尚未核实。
2. Comet-RAG 继续使用原生 S3 SDK。生产尚无证据写入配置，不把来源下载的
   `infrastructure_config.s3` 当成证据配置：它现在只装配 S3Loader。证据小引用还须
   能解析固定存储身份，不能随默认端点变化而指向同名异物；具体接线另行评审。
3. 应用只保存三个对象的包、SHA 和有界恢复意图，不自制共识、分布式锁或存储内核。
   所有候选接受同一套发布与读取契约，不增加为某后端放宽断言的分支。
4. 独立演练可在一台机器使用上游单机启动方式和新目录；这只验证协议，不宣称高可用。
   真正 HA 需先拿到独立主机／磁盘故障域、容量增长、RPO 和 RTO，再定拓扑。
5. SeaweedFS HA 需覆盖 Master 的原生 Raft、Volume 副本，以及 Filer 元数据／路由和
   gateway 失败。优先评估原生 PostgreSQL filer store 复用既有技术栈，但数据库、角色、
   迁移与备份独立；“已有 PostgreSQL”不证明元数据 HA 或并发语义自动成立。

## 隔离验收顺序

| 阶段 | 具体交付 | 不得据此跳过 |
|---|---|---|
| 制品 | 固定 release、commit、镜像 digest／二进制 SHA、工具链及配置快照；保留上一个可恢复制品 | 不用 latest，不把 tag 当不可变摘要 |
| 单端点协议 | 复用 S3 集成开关，条件创建／重复／异字节冲突、checksum、取消／超时、有界回读、7／60 页回放 | 任一不兼容即停止，不用 HEAD→PUT 或无条件回退 |
| 并发与切换 | 两个独立 gateway／client，实际 owner／filer 路径，写入中断与响应丢失，故障恢复后逐字节核对 | 同一 gateway 两连接不等于覆盖 gateway／元数据切换 |
| 数据耐久 | 元数据及 blob 联合备份，空环境恢复，损坏／缺盘／副本不足时拒绝写入 | HTTP 200 或进程重启不等于断电验收；元数据单独备份不等于完整备份 |
| 安全及运维 | TLS、最小应用权限、独立验收管理权限、告警、容量、恢复时间、补丁责任人 | 根凭据不进入应用；对象不可覆盖不等于管理员无法删除 |
| 切换／回退 | 新命名空间受控写入、固定引用路由、切换前后读取与不确定任务恢复 | 不原地换格式、不让生产切流成为第一次验收 |

测试入口沿用[现有 S3 契约方法](mineru_s3_evidence.md)，测试环境变量历史名称仍是
`COMET_TEST_MINIO_*`；这些名字不认证目标是 MinIO，也不授权操作已有资料桶。
写验收必须显式开启 `COMET_TEST_S3_CONDITIONAL=1` 和必需服务模式，skip 不算通过。
实际业务身份只能读写证据范围；创建测试桶的凭据单独使用，不为适配夹具放大生产权限。

## 迁移、回退与维护

首个 MinerU4 生产范围建议只向新证据命名空间写，原有文档与索引不批量搬迁。既有
V1 实验包仅在批准后按清单复制，完整核对身份／长度／SHA／canonical，不重新解析
PDF 来伪装字节迁移。旧 Markdown 和旧 span 不自动升级成新证据账本。

切换前保持新 runner 关闭，先备份并演练恢复。切换后若回退，应停止新提交、排干已
受理任务，保留所有 bundle_intent 与已发布包；不能简单改回旧端点，也不能删除新桶
来“回滚”。新旧引用可解析、旧索引仍可读才算应用回退成功。旧目标如何停写、保留
及退出要先确认其他共享使用者，本轮没有变更其写权限或保留策略。

每月检查维护／安全公告，按实际公告严重性安排验证；候选升级仍跑同一契约与恢复
演练。指定存储维护人和备份恢复责任人，记录制品来源与升级／回退窗口；仅声明社区
活跃不算项目已经有维护责任。新增中间件依赖及实际部署按 spec §7 另行确认。

本方案初版仅文档与选型证据更新；2026-10-28新增上述隔离验收，不外推为生产。初版最近一次完整
unit 为 3850 通过／11.12 秒；它属于前轮 MinIO 诊断，严格 10 秒预算仍待处理。
MinerU 当前阶段及后续顺序见[V1 进度](mineru_v1_design.md#更新进度与下一批范围)。
