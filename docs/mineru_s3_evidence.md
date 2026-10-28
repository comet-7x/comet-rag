# S3 证据适配与目标存储验收

2026-10-28补充：正式组合根在原生目标探测前通过TaskStore持久化许可身份；目标拒绝
则启动失败，旧MinIO门禁未解除。探测不确定性、工厂托管与授权父块读取见
[接线约定](mineru_v1_integration.md)。下面的独立SDK验收命令本身不承担生产编排。

2026-10-28更新：实现迁至`comet_rag.infrastructure.evidence.s3`。SeaweedFS4.51隔离
14项集成、双gateway及崩溃恢复通过；迟到probe仍证明“超时不等于排干”，不启用
对象GC。详见[本批报告](evaluations/seaweed_evidence_20261028.md)。

2026-10-27。**独立适配与故障验证已交付，当前目标MinIO未通过条件发布门禁。**
生产仍不接线。协议主体见[证据生命周期](mineru_evidence_lifecycle.md)，实测与源码指纹
见[适配报告](evaluations/mineru_s3_20261027.md)；目标构建定位与后续隔离故障验证见
[诊断报告](evaluations/minio_diagnosis_20261027.md)。

## 先看目标能力

使用项目既有.env中的目标地址和凭据，仅创建UUID测试桶。普通PUT和正确SHA256
checksum写入成功；错误checksum被400拒绝。但`If-None-Match: *`对不存在key返回
404 NoSuchKey，对已存在key返回200，实际覆盖了测试字节。已分离checksum与条件头
复验，不是仅凭客户端模型有这个参数就认为服务支持。所有探测对象／桶均已精确清理。

AWS原生契约要求不存在时写入、已存在时412；409表示竞争冲突，不能改为无条件写。
见[条件写](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)。
本轮没有通过“HEAD后PUT”、客户端锁、随机换key、换请求头或临时metadata服务绕过
目标缺口。后续官方mc只读查询确认目标为`2023-03-20T20:16:18Z`，提交
`05444a0f6af8389b9bb85280fc31337c556d4300`与官方tag一致：其源码对缺失对象提前
返回404，对`*`只做ETag字面比较，分别解释两项失败。未对远端二进制做字节认证。
SDK发出前确认条件头参与SigV4，签名后删头／改头分别被400／403拒绝，不支持本次
简单丢头解释；这不认证链路没有代理、代理未重签名或全部路由相同。

独立本机参考采用官方RELEASE.2025-09-07T16-13-09Z macOS二进制，SHA与GitHub发布
元数据一致，仅回环地址、随机凭据、临时数据目录，成功路径通过。它不是生产升级
推荐或分布式一致性证明。上游另有[读quorum失败时条件失效问题](https://github.com/minio/minio/issues/21603)
和后续[安全修正版本](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z)；
生产候选必须单独核实维护状态、修正构建和故障语义，不能把健康单机测试推广过去。

后续独立构建上游quorum修正提交`18f97e70b19db6b2940ae8a1b14c4665d2eeca36`，明确
使用DEVELOPMENT标签。候选12项真实集成、原生对象层6个quorum子例、实际源码
反向变异及SIGKILL重启恢复通过，7／60页证据保持canonical；不是官方发布或生产
升级建议。上游已声明仓库不再维护，仍需确定持续维护来源与实际部署验收。

## 适配器职责

`comet_rag.infrastructure.evidence.s3.AsyncS3EvidenceStore`借用应用级原生aioboto3
客户端、固定桶和Gate，符合`ports.evidence.AsyncEvidenceStore`协议。没有默认配置
装配、runner或对象GC，也没有改S3Loader。客户端必须设置`retries={"total_max_attempts": 1}`，避免SDK悄悄
重试不确定写；端点／凭据／TLS仍由调用方明确提供，不自动读取应用配置。

`verify_target()`在公开的本次`probe_key`上做“缺失条件创建→回读→已有对象拒绝覆盖→
回读原字节”。只在所有者的新测试桶使用，所有者须在调用前登记此key供失败清理。
失败后publish明确拒绝；通过只表示当前客户端／桶路径的能力冒烟，不能证明集群
所有故障模式。探测对象不由适配器自行删除，避免取消或丢响应后误判清理结果。

`publish(bundle)`执行：

1. 先复用本地同一份严格字节／语义校验，拒绝不自洽的bundle。
2. 原生单对象PutObject，带`IfNoneMatch="*"`、精确ContentLength、ContentType及
   `ChecksumSHA256`。不用transfer manager或multipart，现有对象上限不需要分片上传。
3. 200或412后都完整有界GET核对长度与SHA，不能仅凭412、ETag或自定义metadata当作
   幂等成功。其他错误不回退无条件PUT；已有异字节对象拒绝并保留现场。
4. 两份数据分别写后核验，再发布manifest。最后回读原生／账本并重新映射通过才返回。

PUT超时／回包丢失报告不确定key，不在同一次调用中自动重提。上游明确412的相同
字节可幂等确认；调用方显式重试仍走条件写与回读，不覆盖、不重调模型。取消保留
CancelledError；被取消的请求仍可能已经提交，故不在异常分支删除证据对象，也不能
把“没有返回成功”当作远端不存在。后续继续按bundle_intent保留身份。

`read(reference)`不要求写能力，可以核验已存在的包。先读取最多64KiB的manifest并
校验外部小引用、身份、文件集合和声明预算，再读取固定两个对象。S3不列举prefix，
只消费manifest绑定的三个key；其他遗留对象不被读取或自动删除。与本地目录的精确
文件集合检查不同，这不声称该S3前缀物理上没有额外对象。

## 有界接收与超时

| 预算 | 独立默认值 | 范围 |
|---|---:|---|
| 单次操作总时间 | 120秒 | 排队、SDK请求、流读取、本地核验共用；不逐对象重置 |
| 单请求时间 | 30秒 | 与剩余总预算取小值；SDK自身connect/read timeout可更短 |
| 原生JSON／账本／manifest | 32MiB／16MiB／64KiB | 复用现有manifest声明与实际字节核验 |
| 操作累计对象接收 | 128MiB | 含重复发布核对及最终回读，不能靠分对象绕过 |
| 每次read | 64KiB | 不调用无界Body.read；越界块不加入结果 |

响应要求200、精确整数ContentLength、JSON媒体类型和无压缩；声明合法也要检查实际
流及SHA。多读一个观察块仍可能计入接收，此限制不是内核硬流量上限。ETag没有被
当作SHA；原生checksum之外，证据始终绑定自己的SHA和canonical。已核验版本的
aiobotocore原生传输关闭自动解压，应用在读对象前拒绝压缩元数据。

GET阶段超时（含SDK原生连接／读取超时）与总deadline分别标记；PUT阶段超时保留
提交不确定性。每条响应流在finally调用原生同步close释放连接，共享client由所有者
关闭。关闭失败不能冒充成功，也不遮蔽原摘要错误：次要关闭失败作为异常note保留，
连接回收仍标记未确认。解析线程复用现有取消排干桥，完成前不释放Gate。

以上是应用层**成功对象body**的预算；SDK内部错误XML、HTTP头和协议解析不计入这个
字节计数，不宣称任意恶意服务响应的硬RSS上限。JSON对象化与本地不可中断工作也
可能使排干晚于deadline；本轮没有复制SDK传输／XML解析层来制造另一个S3客户端。

## 验收方法与下一步

沿用`COMET_TEST_MINIO_ENDPOINT`、`COMET_TEST_MINIO_ACCESS_KEY`、
`COMET_TEST_MINIO_SECRET_KEY`，不要把值写入仓库或命令历史：

```bash
COMET_TEST_REQUIRE_SERVICES=1 uv run pytest \
  tests/integration/test_s3_evidence_minio.py -m integration
COMET_TEST_REQUIRE_SERVICES=1 COMET_TEST_S3_CONDITIONAL=1 uv run pytest \
  tests/integration/test_s3_evidence_minio.py -m integration
COMET_TEST_REQUIRE_SERVICES=1 uv run pytest \
  tests/integration/test_s3_diagnostics_minio.py -m integration
```

第一条明确跳过四项写验收，不能把其结果记为支持条件发布；第二条要求写能力，
不兼容就fail。夹具在PUT前登记精确key，结束逐key删除、删除本次UUID桶并HEAD404
确认，不列举／清理目标其他桶。参考实例测试包含双连接并发，另以单连接池验证
读取取消后的连接复用；“提交后丢回包／延迟返回”通过原生SDK外层注入，服务端确实
已收到PUT，但这不等于已经模拟所有TCP故障或分布式quorum故障。

第三条只验证本次签名头及篡改响应，不能证明写能力或代理拓扑。完整构建身份、原生
quorum探针补丁及隔离验证边界见[诊断报告](evaluations/minio_diagnosis_20261027.md)。

下一步先明确可持续维护的部署候选，再针对真实拓扑演练升级／迁移、TLS/IAM与回退。
本机进程终止和原生对象层故障测试不替代多机故障或断电验收。目标未达标前保留发布
门禁；父块引用保护、证据只读入口和对象GC仍未接线，生产默认与现有索引不变。

后续[持续维护部署方案](object_storage_deployment.md)已确定候选优先序：用户已确认自托管
开源优先，先验收SeaweedFS4.51，RustFS1.0.4备选；接受许可时再考虑AIStor。
Garage官方不支持安全并发条件写，排除。此次是官方版本／源码和部署范围研究，没有
把候选列入生产依赖或运行新的服务验收；独立端点不替换来源S3配置或Milvus内置存储。
