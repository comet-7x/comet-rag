# V2 组件架构

V2 交付独立组件、入库/检索两条异步 Pipeline 和实际运行示例。旧接口、服务和 worker 只保留在旧分支，没有兼容包或服务运行时。各阶段的真实边界见 [验证记录](validation.md)。

| 模块 | 职责 |
|---|---|
| `content.py` / `identity.py` | 有序文本/图片、纯媒体引用、带类型版本的 SHA-256 身份 |
| `sources` | 完整单资源获取、实际 hash、准入和临时文件所有权 |
| `documents.schemas` | 不可变 blocks、表格、富文本、Asset、原生位置和解析报告 |
| `documents.parsers` | 文本、原生 DOCX、单帧图片、显式 MinerU Markdown 协议 |
| `documents.projection` / `exporters` | 从同一快照派生文字坐标和 Markdown，不作为权威缓存 |
| `chunkers` | 保留 Python 字符坐标的切分，结构/媒体原子内容 |
| `indexing` | 自包含 IndexUnit、实际 Representation、Flat 与 ParentChild 构建 |
| `embedder` / `rerankers` | 模态/角色/预算、模型调用、完整身份映射和评分 |
| `docstores` | unit/实际表示/关系与完整写入清单，memory / PostgreSQL |
| `vectorstores` | scope/profile/space 内的 dense/BM25、过滤、明确 IDs 核对 |
| `retrieval` | 单元归并、RRF、原始证据、命中与父上下文 |
| `pipelines` | 两条明确流程、完成状态、重排失败策略与精确删除 |

纯 content/identity/schema 不依赖 SDK、数据库或运行时。模型不导入 indexing，indexing 不导入 stores；SDK 留在所属适配器。`tests/unit/test_architecture.py` 用实际导入图和对应错误注入验证边界，核心 wheel 另在外部进程证明无可选 SDK。

I/O 类型使用 `AsyncXxx` 与普通方法名，纯计算同步。注入客户端由创建者关闭，导入不建连接。阻塞工作放入 `run_sync`：取消和重复取消后等待工作完成，才释放输入并传播取消。批量失败停止派发，取消并排干已启动工作。

## 内容、身份与资源

blocks 是主数据，Markdown/文字投影从当前快照生成。投影范围为 Python 字符 `[start,end)`，绑定 document/projection_version；块间合成分隔符没有原文来源。表格渲染/切分只包含子内容一次。

source_id 是逻辑来源，content_hash 是实际字节；document_id 覆盖输入、引擎/配置和规范化内容；profile 覆盖切分/表示/空间；scope 表示一次隔离构建。unit、representation、space、record 和 manifest 各有确定性带版本编码，不使用 Python hash。实际图片 hash 参与身份，临时路径、凭据和签名地址不参与。

unit 保存可返回内容和必要表格/Asset 闭合；representation 保存实际模型输入。Asset 在下载上下文结束前转存持久目录，源文件只是借用。相同字节移动访问位置不改变内容身份；持久路径与跨机器可读性由调用方保证，不提供资产服务。

## 入库与删除

获取→解析/报告检查→构建 unit/实际表示→嵌入→PostgreSQL 内容与 manifest→Milvus→确认与精确指纹核对。默认拒绝 partial；显式接受后仍保留报告。

PostgreSQL 单文档事务包含 units、representations 与完整 manifest。同一 scope/source 只允许相同快照/profile/清单重试；scope 是一次构建，资料或配置变化用新 scope 完整重建，验证后由调用方切换。

Milvus 每个 scope 独立集合，绑定空间/维度/度量和 metadata schema。prepare 失败保留集合，不自动 drop；Loaded/health 之外执行有界数据查询，处理重启后分片尚未服务的暂时 503。

accepted、durable、query-visible 分开。向量超时可能已写入，保留 unknown 和完整预期 records；按 IDs/内容指纹核对，数量相同不能证明完整。只承诺 PostgreSQL 内部事务，不承诺跨两存储原子性。

删除按 manifest 展开明确 records，确认缺失后删 units/representations，最后删 manifest。任何失败保留后续核对依据，不按 source_id 广泛清理，不自动删除共享 Asset。

## 检索与可选策略

文字默认 hybrid，图片默认 dense。keyword-only 不调用查询嵌入；图片关键词需要显式文字，不隐式 OCR。scope/profile/space 和用户过滤在 limit 前执行，非法过滤不得被忽略或降级。

每通道按最佳表示给 unit 连续名次，保留全部原始表示证据，再用 RRF 融合 unit 排名。同 unit 多表示不重复加分，RRF 不是概率。dense top-k 也不是无关查询拒绝器。

`recall_limit` 限每通道 records，`rerank_limit` 限完整评分集合，`top_k` 限命中 unit，`context_budget` 限返回上下文的文字字符。父块可无向量但有内容；子命中证据与父上下文分开，父读取同范围/快照核验并去重。

重排成功只返回已完整评分集合，失败默认回到原排名并记录诊断，strict 可显式启用。一致性错误在评分前失败；调用方取消始终传播。文本 cross encoder 不支持图片，CLIP 不支持联合图文，适配器明确拒绝，不以自动描述或向量平均伪装。

父子、重排与表示策略的真实小样本比较见 [evals](../evals/README.md)，当前没有证据把父子或重排设为默认。

## 历史与完成边界

[基线 B](https://github.com/comet-7x/comet-rag/tree/75d12489bb6ca96746b370d06623cfe85a64fa41)与[参考 E](https://github.com/comet-7x/comet-rag/tree/ef5e94cdeaafb836ffae03d57063911f6cae5ba1)仅用于算法和边界行为参考。没有整体合并旧库、迁移旧持久数据或保留兼容接口。

S3/MinerU 真实服务、本轮图片服务重启仍未验收；PPTX、整页截图、回答生成、在线版本激活、GC 和持久任务不在当前交付。详细设计在 ignored `tmp/refactoring/overview.md` v1.3，正式使用和验证以本目录及实际示例为准。
