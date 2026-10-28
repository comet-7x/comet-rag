# SeaweedFS 4.51 隔离验收与正式证据适配

2026-10-28。**固定 SeaweedFS 候选通过本机协议、双 gateway 与进程终止恢复验证；
可复用实现已迁入正式包，父块引用保护已实现，未切换生产配置。** 单机隔离验收不
等于真实多机部署已获准。数据与摘要见[机器记录](seaweed_evidence_20261028.json)。

## 先整理既有成果

提交前全量 unit 3850 passed／16 skipped／1 xfailed，11.53 秒；Ruff、Pyright 通过。
原有成果按依赖与职责分为四批提交，没有把实验批次当成生产启用：

| 提交 | 内容 |
|---|---|
| `6791348` | 原生结构映射、有界 V1 采集、对应回归及 4.x 实测报告 |
| `b0857f4` | 不可变证据包、TaskStore 恢复意图、页内标题实验及协议演练 |
| `e9603ec` | S3 条件发布、有界回读、MinIO 诊断与故障证据 |
| `9e61c70` | 开源存储候选、使用说明与 V2 后续计划 |

随后正式模块迁移提交为 `eeade92`，父块引用保护提交为 `a06e229`；本报告单独保存
验收与发布范围，不将提交代码等同于启用生产。

## 固定候选与真实协议

采用[官方 4.51 release](https://github.com/seaweedfs/seaweedfs/releases/tag/4.51)的
macOS ARM64 制品。归档 44,107,366 字节，SHA256
`80000b9ee9129194740d5a766851a536ab8b14c46d18895399fc4bbef2d53c8d`，与发布元数据
一致；原生 `weed version` 报告 commit `153ab668c99731a6d29492bf6c61d0b4fa701101`。
不是从未固定的 master 构建。应用依赖、默认配置与现有 MinIO／Milvus 数据目录均未变。

使用上游 `weed mini`，独立新数据目录与随机 S3 凭据；关闭 telemetry、WebDAV 和
Iceberg／Lance 端口，所有观察到的 TCP 监听均为回环。另启一个原生独立 S3 gateway
指向同一临时 filer。本轮覆盖两个 gateway、一个 filer、一个 master、一个 volume
进程所在的单机环境，**没有覆盖多 filer owner 切换、跨主机 quorum、断电或介质损坏**。

最终原生服务集成 **14 passed，零 skip，5.16 秒**，包括：

- 缺失对象条件创建、已有对象拒绝覆盖，checksum 及有界完整回读。
- 错误 SHA256 被 400 拒绝，HEAD 确认没有创建该对象。
- 两个独立 gateway 同时发布同一包，合计只有三个证据对象 PUT 成功。
- 已提交 manifest 回包丢失、PUT 超时／取消、异常对象、流关闭及单连接池复用。
- 原生 SigV4 条件头与签名后篡改的认证拒绝，不以此证明任意代理路径或完整 S3 兼容。

此前冻结的 7／60 页原生产物通过正式模块回放，未重新调用模型，也未上传至生产：

| 样本 | 原生 JSON／账本字节 | 成功证据 PUT | 重复发布与回读 |
|---|---:|---:|---|
| 7 页 | 95,828／44,213 | 3 | 与原回执 canonical 一致 |
| 60 页 | 19,560,609／556,905 | 3 | 与原回执 canonical 一致 |

## 崩溃恢复与迟到探测写

manifest PUT 已由服务完成、尚未返回应用时，对本轮自有 mini 进程执行 SIGKILL。
使用同制品与同目录重启，先只读核验已提交包，再验证写能力并显式重试；canonical
保持一致，新增成功证据 PUT 为 0。取消仍向调用方传播，Gate 释放。此窗口验证的是
进程终止，不是断电或物理磁盘 fsync 的认证。

首轮驱动用 SDK 的 3 秒读取超时，重启后的新能力探测得到 `put_response_unknown`。
ListBuckets 可用不能证明写路径已经就绪。保留该失败记录后，使用协议既定的 30 秒
单请求预算重新演练通过，没有开启 SDK 自动重试或扩大应用 120 秒总预算。

收尾又发现首轮 UUID 桶仍有一个 24 字节的迟到能力探测对象；两个 gateway 的
HEAD／有界清单均确认存在。首轮私有驱动执行过删除，但没有留存当时 HEAD 状态，
因此**不能声称已证明“确认404后重新创建桶”**，也不能把它简化为客户端缓存。
确认的事实是：不确定 PUT 后，原清理尝试没有证明远端排干，后续观察到对象存在。

为此补特征化单测：客户端超时返回时对象尚不存在，原生请求随后提交；保持原
probe key／不确定身份，发布许可仍为关闭，不自动重写或删除。反向移除错误中的
不确定 key，断言失败。生产包及不确定意图继续保留，不启用对象 GC。

本次剩余对象属于完全自有的隔离目录：补偿前登记唯一桶／key，精确删除、HEAD404
及零桶确认；再先停第二 gateway、后停 mini，核验 PID 和全部监听消失，移除数据、
随机凭据与下载分片。没有清理生产桶或将这种临时目录销毁当作通用生产 GC 协议。

## 正式代码的职责

| 位置 | 职责 |
|---|---|
| `ports/evidence.py` | 标准库值对象、严格小引用和 AsyncEvidenceStore Protocol |
| `engines/documents/mineru/structured.py` | 唯一原生结构映射及 canonical，无 SDK／存储依赖 |
| `infrastructure/extractors/mineru_v1.py` | 原生 V1 采集；复用应用级 HTTP client、Gate 和统一 deadline |
| `infrastructure/evidence/` | 本地包及原生异步 S3 发布／回读，不自建客户端或对象 GC |
| `services/evidence.py` | 固定存储别名解析、完整回读身份与父块原文绑定 |
| `services/indexing.py` | 唯一父块写入点与指纹校验，拒绝引用混用、损坏和错 revision |

评测 CLI、故障测试统一调用上述实现，没有复制两套映射器。旧 eval 实现文件移除；
其历史源码仍可由前四个提交复现。`capture_journal` 和 `page_sections` 留在 evals，
分别用于恢复协议和索引质量探索。新增分层守卫禁止正式包导入 evals；移除守卫对应
限制、实际注入 `import evals` 后测试会红。

采集器只负责采集已锁定的 `CaptureSource`，包含外部确认的源大小、SHA 和物理页数；
它还不是对旧 `extract(path)` 的透明替换。旧 HTTP protocol v2 适配器和 DOCX 链保持。
没有新增 PDF 页数猜测器、MinerU SDK 或 GPU 依赖；生产来源锁建立仍需明确接线。
原先采集校验调用评测分块器，现改为共享映射守卫加明确的空正文拒绝，空页及仅图像
解读的整份产物不能发布成功；对应两项回归与反向验证通过。

## 父块引用与恢复

`IndexPlanner.plan(..., evidence=verified)` 是显式内部入口。引用以固定 `storage_id`
和严格 `EvidenceReference` 表达，最多 2KiB；不能提供任意 URL 控制回读目标。
`read_evidence` 仅按持久化别名查找组合方提供的 Store 映射，未知别名失败，不回退默认。

引用只附在父块，参与计划指纹；document／request／chunk metadata 的同名字段均被
过滤，不传播到向量子块或 draft metadata。没有新向量字段、Task 持久化字段或 SQL
迁移，也没有变更 TaskStore／TaskExecutor／AsyncVectorStore 接口。既有父块 metadata
快照、reserve_revision／put_parents／get_parents 及恢复指纹继续复用。

真实 PostgreSQL **32 passed，3.65 秒**：提交父块后丢回包，新连接幂等重放，嵌套引用
快照隔离；注入损坏引用后在 embedding／向量写入前拒绝，恢复读取后到 ready。
既有 DocumentStore 契约及原恢复协议一并通过。

反向验证覆盖请求伪造、父块正文与 revision、来源／canonical、存储别名、向量泄漏、
部分引用、不确定 probe key、空正文及包边界，共 **12 个实际源码缺陷均检出**。
最初错 revision 用例只改第一个父块，会被“引用混用”守卫掩盖；修正为所有父块均
引用同一错误 revision，并构造指纹自洽的错误载荷后，真正检出了移除 revision 守卫。

## 交付与剩余范围

新 wheel 在全新 core-only 环境、仓库外完成映射与 V1 模块导入冒烟；MinerU、Torch、
aioboto3、pymilvus、SQLAlchemy、Redis 均未安装，wheel 不包含 evals。离线构建首次因
旧 uv 缓存文件缺失失败，改用独立缓存完成构建，没有改依赖锁。

最终 unit **3920 passed／16 skipped／406 deselected／1 xfailed，10.12秒**；Ruff、
Pyright和29个改动Python文件格式检查通过。E2E 50 passed，benchmark 39 passed。单测历史
11.53／10.60／10.04 秒记录保留，不挑最好一轮来关闭严格 `<10s` 门禁。没有重跑
全部真实 Milvus／MinerU／分布式 worker 集成，也不从本轮结果推导新的合并覆盖率。

本批完成可安装的适配实现和受保护的索引入口，**未注册默认 MinerU4 runner、配置
端点或 HTTP 证据路由**。下一批应明确来源锁建立、发布前持久化意图与发布许可、
已知ID恢复、保留／告警，以及父块引用到只读入口的授权；再做新命名空间端到端与
回退。页内标题质量、独立标签、不可变模型工件和真实部署验收仍不关闭。
