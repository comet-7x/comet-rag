# V2 组件架构

V2 以独立组件、入库/检索两条异步 Pipeline 和实际运行示例为交付。阶段 0 与 A 已完成：`content`、`identity`、`documents.schemas`、`documents.projection`、`documents.codec`、ZIP/XML 资源检查和基础 RRF 已实现。其他模块按下表职责继续建设；[阶段 A 核验](phase-a.md)记录契约与环境的实际边界。

| 模块 | 后续职责 |
|---|---|
| `content.py` | 有序文本/图片输入和媒体引用，无网络或文件读取 |
| `sources` | 获取单个完整资源、来源身份和临时输入所有权 |
| `documents.schemas` | 规范化 blocks、Asset、位置、解析报告与快照 |
| `documents.parsers` / `exporters` | 原生结构解析与派生导出；资源预算先于解析 |
| `chunkers` | 保留来源坐标的边界切分 |
| `indexing` | 物化自包含单元与实际检索表示，固定身份及关系 |
| `embedder` / `rerankers` | 模型输入、角色、批量身份映射和结果校验 |
| `docstores` | 内容快照、实际表示、关系和持久写入清单 |
| `vectorstores` | 明确 scope/space 内的 dense 与关键词召回 |
| `retrieval` | 通道内单元归并、RRF、证据和上下文组织 |
| `pipelines` | 完整入库和搜索编排、实际完成状态与失败策略 |

不为上表创建占位目录。首套持久后端为 PostgreSQL + Milvus，SDK 留在适配器；内存后端用于契约测试。中性内容与纯数据不依赖 SDK，模型不反向导入 indexing，indexing 不调用 stores。I/O 类型使用 `AsyncXxx` 与普通方法名，纯计算同步。

## 入库与读取边界

规范化 blocks 是主数据，Markdown 是导出视图。文本空白、parts 次序和 Python `[start,end)` 字符范围保留，范围绑定文档快照及投影版本。内容、身份、表示和空间分开编码，使用带类型/版本的确定性序列化与 SHA-256。

index builder 物化可返回内容，representation builder 保存实际模型输入。资源写入调用方提供的持久目录，先转存再释放下载输入；客户端和持久资源由创建者管理。

PostgreSQL 在单个文档事务中保存 units、representations 与完整预期向量清单，再写 Milvus。响应丢失或超时保留未知状态，按明确 ID 核对和重试；相同 count 不代表写入完整。这里只承诺数据库本地事务。

资料更新在新的 `index_scope` 重建，完成清单和可见性验证后由调用方切换。首条 Milvus 路径每个 scope 使用独立集合。删除从清单展开明确 IDs，先确认向量删除，再清内容，最后删除清单；失败保留核对依据，资产不随单元自动删除。

## 检索边界

保留 dense、keyword、hybrid，keyword 无需查询嵌入。纯图片默认 dense，关键词需要调用方显式文字视图。scope/space 和用户过滤在候选截断前执行，不在失败时移除约束。

各通道按 unit 的最佳表示归并成连续排名后再 RRF。当前 `reciprocal_rank_fusion` 只消费已排序 unit_id，重复 ID 不贡献额外分数，也不占用后续名次；它返回融合分数和各通道单元名次，原始记录分数/表示证据由后续归并组件保存。同分按 unit_id 排序。

重排成功只从完整评分集合返回，失败允许回到原融合排名并报告；一致性错误和取消始终传播。命中单元与扩展上下文分开，dense top-k 不被描述为相关性拒绝器。

## 参考实现

旧实现仅通过 Git 历史参考，不在当前包保留兼容路径：

- [基线 B：75d1248](https://github.com/comet-7x/comet-rag/tree/75d12489bb6ca96746b370d06623cfe85a64fa41)：原生 DOCX/OMML、ZIP/XML 预算、来源下载、模型协议、Milvus BM25、RRF。
- [后续参考 E：ef5e94c](https://github.com/comet-7x/comet-rag/tree/ef5e94cdeaafb836ffae03d57063911f6cae5ba1)：阻塞工作取消排干、结构切分、批量映射、过滤、metadata 隔离、非破坏性集合准备与 PostgreSQL 存取。

阶段 A–F 逐项适配上述行为，不搬入旧任务、版本激活或服务运行时。阶段 0 检查不代表真实解析、持久化、模型或检索质量验收。完整本地设计仍以 `tmp/refactoring/overview.md`（v1.3）为准。
