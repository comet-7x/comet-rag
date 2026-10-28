# MinIO 构建定位与隔离兼容候选验证

2026-10-27。**目标实例的条件写不兼容来自已核实的旧版实现；本轮候选通过限定范围的
隔离验证，仍不构成生产升级建议。** 没有升级目标服务、切换配置或接线生产证据写入。
承接[上轮 S3 验证](mineru_s3_20261027.md)；[机器记录](minio_diagnosis_20261027.json)
保存构建、摘要、测试结果和边界，不包含目标地址、凭据、Authorization 或拓扑。

## 目标版本与代理证据

使用官方 `mc admin info` 的只读结果，确认服务运行时报告：

| 项目 | 核实结果 |
|---|---|
| version | `2023-03-20T20:16:18Z` |
| commitID | `05444a0f6af8389b9bb85280fc31337c556d4300` |
| 官方 tag | `RELEASE.2023-03-20T20-16-18Z`，提交号吻合 |

这比 HTTP Server 头或仓库 compose 标签更精确，但仍是运行时自报身份加源码行为核对，
没有获取远端二进制做字节认证。管理接口的加密响应交给官方 mc 处理，没有复制其解密
协议。mc 使用官方 macOS ARM64 资产，SHA256 与发布元数据一致；临时配置权限为
0700/0600，命令结束删除。记录只投影 version/commit，原始管理结果不写入仓库。

代理验证使用原生 aioboto3 的 `before-send.s3.PutObject` 钩子：观察完成签名后的条件头，
再对本轮 UUID 桶的独立 key 注入删头／改头。SDK 重试关闭，显式 `proxies={}`：

| 请求 | 签名前提／服务响应 |
|---|---|
| 原样发送 | `If-None-Match: *` 存在且列入 SigV4 SignedHeaders；404 NoSuchKey |
| 签名后删除条件头 | 400 AccessDenied |
| 签名后改条件头 | 403 SignatureDoesNotMatch |

这些观测**不支持本次请求的条件头被简单丢弃／改写**这一解释。客户端未配置环境代理
不等于链路没有代理；仍未认证物理代理拓扑、代理重签名或所有路由行为一致。源码与
响应吻合，使服务原生实现成为有证据支持的原因，不需要靠猜代理解释失败。

## 精确提交如何解释两种失败

检查的是上述完整 commit，不把后来 2025 年的 quorum 问题混为同一缺陷：

1. 新 key 返回 404：旧版条件 PUT 的预检调用 `getObjectInfo`，遇到不存在对象错误时
   提前返回；它只豁免 version-not-found，没有豁免 object-not-found。因而缺失对象的
   条件创建还没进入正确的缺失判断就结束了。见
   [erasure-object.go](https://github.com/minio/minio/blob/05444a0f6af8389b9bb85280fc31337c556d4300/cmd/erasure-object.go#L999)。
2. 已有 key 被覆盖：HTTP handler 会注册条件检查，但旧版 `checkPreconditionsPUT`
   对 If-None-Match 直接做 ETag 相等比较，没有为 `*` 实现“任何已存在对象都拒绝”的
   通配语义；实际 ETag 与字面 `*` 不等，写入被放行。见
   [handler](https://github.com/minio/minio/blob/05444a0f6af8389b9bb85280fc31337c556d4300/cmd/object-handlers.go#L1770)
   与[比较实现](https://github.com/minio/minio/blob/05444a0f6af8389b9bb85280fc31337c556d4300/cmd/object-handlers-common.go#L134)。

上轮已经实测“新 key 404、已有 key 200 且字节变化”；本轮版本、签名头和源代码补齐
原因链。本轮目标集成 8 通过、4 项条件发布验收明确跳过；门禁仍拒绝发布，不能把跳过
计作写能力通过。无需改为 HEAD→PUT、无条件回退或自制客户端锁。

## 隔离候选的身份与维护边界

先检查[条件写读 quorum 问题](https://github.com/minio/minio/issues/21603)与
[上游修正 PR 21653](https://github.com/minio/minio/pull/21653)，再构建合并后的完整提交
`18f97e70b19db6b2940ae8a1b14c4665d2eeca36`。它是**固定源码的实验候选**，不是官方发布包，
也不是本轮批准的部署版本。此前 2025-09-07 健康参考端的成功不能替代这些故障验证。

- 原生 Go 1.24.8 ARM64 工具链独立解压，官方 SHA 核对；依赖使用 go.sum 和
  GOSUMDB 核验，未关闭校验或改锁文件。官方模块代理连接超时后仅本实验改用镜像，
  不修改项目依赖或全局 Go 环境。
- `go build -p 2 -mod=readonly -trimpath -buildvcs=false`，按上游版本变量写入完整
  commit；标签明确为 `DEVELOPMENT.2025-10-24T04-05-31Z`，时间取合并时间，不能
  误读成官方 release。二进制 SHA256：
  `02cdfda9049ca533c4efa5ceef13eead21ccae98255b53a8a2cc3e9373b6caae`。
- 官方 mc 回读该隔离实例的 version/commit 与构建记录一致。只绑定
  `127.0.0.1:29021/29022`，随机凭据、独立目录，没有连接生产数据目录。
- [上游当前维护说明](https://github.com/minio/minio/blob/7aac2a2c5b7c882e68c1ce017d8256be2feea27f/README.md)
  已声明该仓库不再维护。即使固定提交通过功能测试，也不能称为“维护中的生产候选”；
  实际部署还需明确持续维护来源、当前工具链／安全修正、制品和回退责任。

## 故障、恢复与反向验证

候选原生服务集成 **12 passed、零 skip**：缺失创建、已有拒绝、双连接并发、相同字节
重复发布、manifest 提交后回包丢失、提交后超时／取消、异常对象、读取取消及单连接池
复用。故障注入位于 SDK 调用边界，服务确实完成写入；不冒充完整网络丢包模拟。

另在 manifest PUT 已提交、应用尚未收到结果的窗口，对**本机自有候选进程**执行
SIGKILL；调用方取消保留且 Gate 释放。相同二进制与数据目录重启后，由新 client
重新验证能力并显式重试：三个证据对象均已存在，新增成功证据 PUT 为 0，完整回读
canonical 一致。这只验证该进程终止窗口，不认证主机断电、磁盘 fsync 或跨主机持久性。

quorum 验证复用上游 `prepareErasure16`、StorageAPI 和 ObjectLayer，而非另造存储协议：

- 原生 `TestPutObjectConditionalWithReadQuorumFailure` 三个子例通过。
- 新[验收探针补丁](minio_quorum_probe_20261027.patch)仅对自有对象的 9/16 个盘注入
  元数据读取失败，其他方法委托原盘，保留写能力；先断言 GetObjectInfo 确实触发
  read-quorum 错误，再验证 none-match、正确 match、错误 match 三类条件写均拒绝。
  恢复读取后逐字节确认原对象未变。两项顶层测试共六个子例通过。
- 实际反向变异删除候选 `erasure-object.go` 中条件预检的读取错误守卫，三个子例
  **全部失败，并观察到原字节被覆盖**；恢复原源码后全部通过。源码已与下载归档
  重新核对，生产候选二进制不含变异。
- 初版只注入 ReadVersion 时，自检发现最新版本路径走 ReadXL，故障没有生效；该
  结果不计作有效验证。补齐两条原生路径后重跑基线／变异／恢复，避免测试绕过目标。

此处 quorum 故障发生在原生对象层测试，**不是运行中多机集群的磁盘／网络故障验收**。
探针复现方法：在上述精确候选源码根应用补丁（`patch -p1 < ...patch`），使用同版本
Go 工具链和独立缓存执行：

```bash
CGO_ENABLED=0 GOMAXPROCS=2 go test -p 2 -parallel 1 -mod=readonly ./cmd \
  -run '^(TestPutObjectConditionalWithReadQuorumFailure|TestCometConditionalQuorumRecoveryPreservesBytes)$' \
  -count=1 -timeout=120s -v
```

7／60 页既有原生产物仅回放到本机候选，重复发布和独立有界回读与原采集回执一致：

| 样本 | 原生 JSON／账本字节 | 每包成功证据 PUT | canonical |
|---|---:|---:|---|
| 7 页 | 95,828／44,213 | 3 | 保持一致 |
| 60 页 | 19,560,609／556,905 | 3 | 保持一致 |

没有重新请求模型，也没有向目标服务器上传用户 PDF 或其解析证据。

## 仓库改动与最终验证

新增 `evals/pdf/s3_diagnostics.py` 两类纯诊断：官方 mc JSON 的有界构建投影，及 SDK
签名头／故障响应事实归纳。512KiB、128 节点、严格日期／commit／重复字段检查；
保留混部身份，仅返回白名单字段，不保存管理结果或签名。不新增生产 Port、网络
客户端、任务协议或自动运维入口；诊断输出也不替代服务可信认证。

- 新增 26 项零网络单测；12 项诊断源码变异均使目标断言失败，恢复原源码后通过。
- 采集、结构、证据、恢复、S3 及新诊断共 **215 passed，0.94 秒**。
- 全量 unit **3850 passed、16 skipped、403 deselected、1 xfailed，11.12 秒**。
  严格 `<10s` 预算仍未达标，不能把定向速度或过往最好一轮当作关闭证据。
- 目标集成 **8 passed／4 skipped，1.81 秒**；隔离候选 **12 passed，1.27 秒**。
- 全仓 Ruff、Pyright、三个新 Python 文件格式检查通过。生产源码、依赖锁和
  配置无改动，私有 `.env` 与既有指纹一致。未重跑不受本批影响的全套中间件／E2E／
  benchmark，未沿用历史覆盖率冒充本轮结果。

目标 UUID 桶逐 key 清理并 HEAD404 确认；候选清理前零桶，随后 SIGTERM 退出、两个
监听关闭，临时数据／凭据目录移除。保留固定源码、核验二进制和日志供复现。

下一步先明确可持续维护的部署候选，再针对真实拓扑演练升级或新命名空间迁移、
TLS/IAM、故障与回退；现有目标能力门禁继续保留。父块引用保护、证据只读入口、
对象回收与迟到写对账仍未接线，本次不提前启用。
