# MinerU V1 接线与分支收口验收

2026-10-28。约定四组工作已经完成并分批提交，可进入代码审查；**功能检查通过，
严格10秒单测预算仍未通过，生产发布门禁未关闭。** 未合并develop、未移动目录、
未推送或切换生产配置。实现和测试对应的代码版本是
`d5474b3331ebaee7948b89e3e390c24a686819d0`，图与本报告随后独立提交，避免自引用。
机器结果、31项变异及43个源码/测试摘要见[机器记录](mineru_wiring_20261028.json)。

## 本轮实现提交

| 提交 | 职责 |
|---|---|
| `680224c` | fix(docx): preserve valid namespace and field result semantics |
| `4eb88c8` | fix(tasks): tolerate one transient lease renewal timeout |
| `90dc68f` | fix(ingest): fence source writes and reject empty replacements |
| `3384fdb` | feat(evidence): persist publication permits and fenced MinerU recovery |
| `693255f` | test(ingest): distinguish independent documents from source conflicts |
| `d5474b3` | feat(mineru): wire opt-in revision capture and authorized evidence reads |

验收文档与生成图另行提交；本表不自引用其自身提交。审查本轮范围可从`d3fd7a8`起对比。

## 已完成的范围

- 发布许可：原生S3条件探测前持久化目标/probe身份，CAS领取后验收，再确认许可。
  不确定探测保留且不自动重放，历史成功不能授权新SDK；复用TaskStore，无新表/字段。
- 正式接线：受控来源锁、POST前意图、远端身份、来源核验、精确manifest意图/确认
  接入既有revision阶段。已提交包优先只读恢复；坏包/403/超时不能回退重采。
- 生命周期：异步工厂每循环创建/登记资源，启动失败等待回滚，清理错误不覆盖原错；
  成功后Context托管，执行器排干后关闭连接；重复取消和并发close有故障覆盖。
- 授权读取：可选HTTP入口先核token和KB，再判活、验包及核对父块原文；不返回原始
  资产或存储地址，不回退未核验正文。其他参考API没有因此获得全局鉴权。
- 审查确认的修复：flat同源写围栏、空提取/恢复空draft/零child拒绝、有限metadata、
  非法降级阈值、单次慢续租、DOCX默认命名空间/未闭合字段/SEQ误识别。普通分块复用
  已有覆盖守卫，移除本轮重复检查；没有为降低行数而另造抽象。

详细操作与失败处置见[接线约定](../mineru_v1_integration.md)、
[入库安全](../ingest_write_safety.md)。外部审查20项逐条复核及延期范围见
[审查记录](audit_recheck_20261028.md)；legacy判活、cron游标与失效文档路径的原报告
推论已校正，不盲目照单修改。

## 最终验证

| 检查 | 最终结果 | 口径 |
|---|---|---|
| 普通默认unit | 4076 passed / 16 skipped / 1 xfailed，12.58s | 无coverage；10秒门禁未通过 |
| unit覆盖率运行 | 4076 passed，18.26s，90.77% | instrumentation耗时不与10秒混用 |
| 完整integration | 321 passed / 3 skipped / 60 warnings，521.55s | 本机隔离PG/Redis/Milvus/S3；不是生产 |
| E2E | 50 passed，2.87s | 全量内存ASGI/执行器路径 |
| benchmark | 39 passed，7.45s | 生成夹具/替身模型，不是生产QPS或模型质量 |
| 三层合并覆盖率 | 93.56%；tasks 96.32% | 独立unit/integration/e2e数据合并 |
| 源码反向验证 | 31项缺陷均被检出，原始字节恢复 | 并发/空输入/协议/顺序/鉴权/生命周期/背压 |
| 静态 | Ruff / Pyright通过 | 项目既有全仓范围 |
| wheel / core-only | 新建环境、仓库外DOCX/分块/原生结构冒烟通过 | 无GPU/服务extras；199个wheel模块与代码版本相同 |
| 提交切片 | 入库安全784项、证据协议830项通过 | 从暂存索引导出后检查，不借用后续未提交模块 |

unit跳过项为1项明确opt-in本地真实DOCX和15个未引用项目符号的文档块；原有1项
xfail保留。集成3项跳过是未配置旧MinerU HTTP服务的实测，新V1的真实PG/S3用例均
执行。60项警告包括59项上游ARQ/Redis close弃用和1项JUnit属性兼容警告。

首次E2E/吞吐执行揭示旧夹具将多文档提交成同一source，新增围栏使其快速失败，
没有实际压到队列/模型。修正为不同逻辑source，复用同一生成文件字节，并加强精确
受理数/业务记录数及20个成功source唯一性断言。没有删除失败测试、放宽429/吞吐
成功条件；关闭准入实现后两条背压E2E均失败。同源竞争另有unit及PG跨连接验证。

普通unit最慢仍是并发窗口、真实等待边界和子解释器导入。完整运行12.58秒，不能用
单个定向用例快、coverage轮次或挑选最快一轮来关闭10秒门禁；本轮不继续扩展性能
改造范围。最终原始日志保留在私有`/tmp/comet-mineru-closeout-20261028/`。

## 真实PDF与中间件边界

固定既有公开7页PDF，独立MinerU4.0.12 API连接9919的Pro2605模型，通过ASGI、原生
ARQ CPU/IO实例、PG、Milvus2.6.23和SeaweedFS4.51走完提取/准备/索引/激活/清理。
耗时11.68秒、7个parent/11个child、3个成功业务证据PUT；无token读取401，授权读取
200。embedding用确定性替身，CPU/IO为同一Python进程内的独立worker实例，文档API
另起进程；不是新一轮真实embedding质量或跨主机验收。

本次运行包含显式SDK预探测；“首次原生探测前持久化”的保证由独立真实PG/S3故障
测试验证，不能仅靠这条成功链自证。工厂资源所有权及API/CPU/IO入口另有直接测试。
本轮没有重跑60页质量评测，也没有切换默认标题/分块/检索策略。

同源PDF两轮真实解析的canonical分别为`1422341b…`与`b929253c…`，完整摘要见机器
记录。仅通过已知原任务ID回读冻结后者，未为恢复重发POST。页数仍为7；没有将旧span
套到新正文，也没有把字节/页完整与小样本成功当作OCR质量稳定。模型工件尚未固定，
变化原因/质量收益未做新实验，保留独立验收门禁。

原compose全量拉取旧MinIO镜像受registry拒绝的限制，本轮沿用缓存官方Milvus
嵌入etcd/本地存储隔离形态；未修改compose或替换Milvus生产内部存储。S3为固定
SeaweedFS4.51、单filer/双gateway。目标旧MinIO门禁继续拒绝，不能将隔离候选结果
写作目标部署通过。

## 清理与审查顺序

自建MinerU任务1个已终态，拥有的S3桶剩余0；自建API/SeaweedFS进程、数据及凭据
已清理，Milvus容器移除，本地PG/Redis恢复停止、原卷保留。9919模型及用户生产服务
未动。`.env`匹配此前冻结配置，config.yaml/config.example.yaml/compose/依赖锁未改。
新core-only冒烟出现ONNX telemetry标识持久化警告；MinerU关停仍出现一次semaphore
清理警告，不宣称零告警。

先审查本分支提交和[交互图索引](../diagrams/README.md)，经用户决定再合入develop；
之后另开目录重构。本轮不自动合并、调整目录或追加新功能。以下门禁继续保留：

1. 严格10秒unit预算及目标CI复验。
2. 未参与出题者的独立标签裁决、固定权重/tokenizer/运行时不可变身份与标题质量对照。
3. 实际存储/部署拓扑、TLS/IAM、容量、备份、真实资料迁移、切换与回退。
4. 不确定意图的独立归档、probe与证据对象保留/配额/GC及运维处置；不能按年龄猜排干。
5. 参考服务的全局认证或可信网关验收、上游semaphore生命周期问题，以及审查记录中
   明确延期的DOCX覆盖/通用本地大小/列表计数与配置语义事项。

这些门禁未通过不等于代码未写完，也不等于获准生产发布。用户审查本次默认关闭的
实现与随后目录调整，可与实际生产迁移/独立质量验收分开安排。
