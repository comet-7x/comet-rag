# Spec: M4 Chunking 与层级索引

> 状态：已冻结，M4-T2 已完成（v1.0）
> GitHub Issue：[#57](https://github.com/comet-7x/comet-rag/issues/57)
> 开发分支：`feature/m4-chunking`
> 最后更新：2026-09-14

## 1. 目标

M4 把当前“按文件类型返回 `list[str]`”的分块实现升级为可追溯、可替换、可评测的
Chunking 子系统，并在平坦分块稳定后增加可选的父子索引。最终应同时满足：

1. 库用户可以只使用固定、递归、标题或页面策略，不安装服务依赖；
2. 每个块保留来源位置和文档元数据，下游不再靠 `chunk_index` 猜上下文；
3. 分块算法、外部模型调用和索引编排各守自己的边界；
4. 现有 `Pipeline`、任务入库和 dense/keyword/hybrid 检索在迁移期间保持兼容；
5. 参数是否改善检索由固定样本评测回答，不用默认值或主观示例代替证据。

## 2. 非目标

- 不在 Chunker 中实现 GraphRAG、实体关系抽取或图存储。
- 不让 Chunker 访问 Milvus、PostgreSQL、模型 HTTP 服务或供应商 SDK。
- 不把 MinerU 的原始响应字段直接暴露为通用 Chunk 契约。
- 不新增 tokenizer、NLP 或向量模型依赖；token 计数器由调用方注入。
- 不在同一提交中同时修改 Chunk 契约、数据库迁移和检索行为。
- 不自动删除或迁移已有 Milvus collection。
- 不承诺首版支持所有代码语言和文件格式；先保证通用 Markdown/DOCX/PDF 链路。

## 3. 现状审计

当前实现有四个结构性问题：

1. `ChunkHook = Callable[[str, PipelineConfig], list[str]]` 丢失了
   `NormalizedDocument.metadata`、页码、标题路径和原文位置；Pipeline 只能在事后补一份
   文档级 metadata。
2. `RecursiveCharacterTextSplitter` 只用 `len()`，`chunk_size` 和
   `chunk_overlap` 实际都是字符数，却没有在公开配置中说明计量单位。
3. overlap 按完整 split 回收。自然分隔单元大于 overlap 时，配置为非零也可能得到零
   重叠；现有测试已把这个局限特征化，但结果中无法观察实际重叠。
4. 分块产物存在两种形态：engine 返回 `list[str]`，Pipeline 再构造带 ID、metadata 和
   embedding 的 `Chunk`。任务链路又把纯字符串列表跨 worker 持久化，两个入口容易漂移。

## 4. 开源实现调研

### 4.1 对照结论

| 项目 | 可借鉴设计 | 不直接照搬的部分 |
|---|---|---|
| LangChain | 通用递归 splitter 逐级尝试段落、换行、空格和字符；长度函数可注入；纯文本与 `Document` 输出分开 | `add_start_index` 通过切完后 `find()` 回查，在 token overlap 与重复文本上出现过 `-1`；本项目必须在切分过程中携带位置 |
| LlamaIndex | NodeParser 直接消费 Document 并继承 metadata；文本、文件结构、层级和语义 parser 分包；父子关系显式建模 | `SemanticSplitterNodeParser` 自己持有 embedding model，使纯算法与外部资源耦合，不符合本项目 Port/Strategy 边界 |
| Haystack | DocumentSplitter 输出仍是 Document，保留 `source_id`、页码、起始位置与 overlap 信息；层级 splitter 是独立组件 | Haystack 的通用 Document 适合组件平台，本项目不需要把所有阶段压成一个万能对象 |
| RAGFlow | 解析后保留 PDF 位置、图片和父块信息；token 与 title chunker 可串联；生产链路重视布局事实 | Chunk 以自由 `dict` 传播且 parser/chunker/index 字段耦合较深，本项目继续使用有类型的值对象与分层契约 |

### 4.2 采用原则

1. 采用 LangChain 的递归优先级和可注入长度函数，不复制其事后搜索位置的做法。
2. 采用 LlamaIndex/Haystack 的“文档进、带 metadata 的块出”和独立层级规划。
3. 采用 RAGFlow 对版面事实的保留，但这些事实必须先由 Extractor 映射为通用字段。
4. 不引入上述框架作为运行时依赖；实现保持 core-only，测试用其公开行为作对照而非逐行
   fork。

## 5. 已确定设计

### D1 — ChunkingStrategy 是同步纯计算策略

```python
class ChunkingStrategy(Protocol):
    def split(self, document: NormalizedDocument, /) -> list[ChunkDraft]: ...
```

- 输入是完整 `NormalizedDocument`，不再只传 Markdown 字符串；
- 输出 `ChunkDraft`，至少包含 `text`、`ordinal`、`start_char`、`end_char` 和
  块级 `metadata`；位置无法无损表示时允许为 `None`，不能伪造；
- Strategy 不生成依赖 `source_id` 的最终 ID，不持有 embedding，也不做 I/O；
- 它只有同步方法。服务的 async 路径继续统一使用一次 `asyncio.to_thread()`；为纯 CPU
  算法增加 `asplit()` 只会制造两份接口。

现有面向用户的 `Chunk` 仍表示 Pipeline 的完整结果。Service 将 `ChunkDraft` 加上稳定
ID、来源 metadata 和可选 embedding 后构造 `Chunk`，避免一个对象同时表示“待索引”和
“已索引”。

metadata 合并顺序固定为“文档 < 请求 < 块级事实 < 系统字段”。非系统层提供的
`source_id`、`chunk_index`、`parent_id` 等保留键会被丢弃，而不是在系统层缺少该键时
侥幸保留；这样未来新增父子索引时不会把调用方输入误当成可信关系。

### D2 — 位置在切分过程中产生，绝不事后 `find()`

递归拆分的内部单元携带字符区间，合并时直接计算输出区间。这样重复段落、token 计量和
overlap 都不会让 `start_char` 指向错误位置。对连续文本块必须满足：

- `document.markdown[start_char:end_char]` 与块的来源正文一致；
- ordinal 严格递增；
- 无 overlap 时按位置重建原文，不丢、不改、不重排；
- 空白输入不产生块；任何非空块都不超过配置预算。

### D3 — 大小与 overlap 使用同一显式计量器

`LengthFunction = Callable[[str], int]` 由构造函数注入，默认 `len` 以保持兼容；文档和
配置必须明确默认单位是 Unicode code point，不笼统写“token”。未来 token 计量由用户
或模型适配层提供，不在 engines 固定 tiktoken 等依赖。

`chunk_overlap` 是同一计量单位下的**目标上限**。优先保留完整语义单元，实际 overlap
可以小于目标；`start_char/end_char` 让实际值可计算，不再静默假装精确。若产品需要强制
最小 overlap，应另增明确模式和质量测试，不能悄悄把句子切断。

### D4 — 格式类降为配置画像，算法实现不按扩展名复制

`TextChunker`、`DocxChunker`、`MdxChunker` 与代码类目前主要差异是 separators 和默认
数字。M4 保留这些公共类作为兼容门面，但核心只实现少量正交策略：

- `FixedSizeChunker`：无自然边界时的确定性兜底；
- `RecursiveChunker`：按 separator profile 递归切分；
- `MarkdownSectionChunker`：尊重标题与代码块等规范 Markdown 结构；
- `PageChunker`：只消费 Extractor 已提供的页边界，不猜页码。

文件类型选择属于 Service/Hook 路由；separator profile 是纯数据，不能继续用大量几乎
相同的子类表达配置差异。

### D5 — 结构事实使用引用规范 Markdown 的 DocumentBlock

M4 需要标题和页面边界时，在 `ports/document.py` 增加最小 `DocumentBlock`。它用
`start_char/end_char` 引用 `NormalizedDocument.markdown`，并只记录真实消费者需要的
`kind`、`ordinal`、可选 `page_number`、`heading_path` 和 metadata。正文只保存一份，
避免 `markdown` 与 block.text 成为两套真相。

- 标题结构可由 normalization 后的 Markdown 分析器产生；
- 页码必须来自 DOCX/PDF Extractor 的事实；没有页信息时 PageChunker 明确拒绝或由
  Planner 选择递归策略，不把换行猜成分页；
- bbox、图片资产和表格单元格坐标不在首版字段中，出现真实消费用例后再扩展。

### D6 — 语义分块不是纯 ChunkingStrategy

Embedding 调用有网络、并发、失败与资源生命周期，因此完整语义分块由
`SemanticChunkingService` 编排 `EmbeddingPort`：

```text
Sentence/Section Strategy → EmbeddingPort → BreakpointStrategy → ChunkDraft
```

只有相似度计算与断点选择进入 engines。该路径必须复用现有批量 embedding 排程和进程级
闸门，并设置硬 `max_chunk_size`；不会在 M4 的基础分块阶段默认启用。

### D7 — 父子关系由 IndexPlanner 产生

`HierarchyBuilder` 消费已经稳定的平坦块，`IndexPlanner` 输出 `IndexPlan`：哪些子块要
embedding、哪些父块只存正文、每个子块回填哪个 parent，以及相邻关系。Chunker 不写
`parent_id`，也不决定检索命中后返回父块还是子块。

首版只做两层：父块用于回填，子块用于 dense/BM25 检索。多层递归、RAPTOR 和 GraphRAG
不在首版范围。

### D8 — DocumentStore 与向量库分开，实施前设置 schema 决策门

父块正文不塞进 Milvus JSON metadata；它由 `DocumentStore` 按
`kb_id/source_id/revision/parent_id` 保存，向量记录只携带有界引用。InMemory 与
PostgreSQL 实现共享契约测试。

该阶段会新增持久化表并改变向量 metadata 约定，属于 `tasks/spec.md §7` 的“先问再动”
范围。M4-T6 只能先冻结接口、迁移和失败恢复方案；得到明确确认后才能执行 T7 数据库
迁移和 T8 双存储入库。

### D9 — 可靠入库使用 revision，不覆盖当前可用版本

层级入库不能继续只用 `source_id:index` 原地覆盖，否则第二个存储写失败时无法回滚。
目标协议是：

1. 为本次文档生成 revision，并使用 revision 相关的父/子 ID；
2. 写入父块和子向量，失败时只清理本 revision；
3. 全部成功后切换 DocumentStore 的 active revision；
4. 检索丢弃非 active revision 的候选，再有界补取；
5. 最后回收旧 revision，回收失败只记录并可重试，不撤销已经激活的新版本。

T6 必须用故障矩阵验证每个断点。若实现证明 Milvus 侧无法以有界代价过滤 active
revision，应停在决策门重新设计，不能降级为“通常不会重复”。

### D10 — 兼容迁移一次完成，不长期维护双链路

- 旧 `BaseChunker.chunk(str) -> list[str]` 与格式 Chunker 在 M4 内保留；内部转调新策略；
- 新增文档级 Hook 时为 `PipelineHooks.chunker` 提供明确适配器，现有自定义 hook 不立即失效；
- `Pipeline` 与任务入库必须最终调用同一个 Chunking Service，不能各自复制 metadata
  合并与 ID 生成；
- 兼容接口标注弃用版本和移除窗口，M4 不保留两份核心算法。

具体迁移入口是 `PipelineHooks.document_chunker`；旧 `PipelineHooks.chunker` 在 T4 新链路
启用后开始发出弃用提示，继续支持整个 0.2.x，最早在 0.3.0 移除。内置 Hook 必须先迁到
新契约，不能让框架自身触发弃用提示。

## 6. 成功标准

### S1 — 契约与分层

- [x] `ChunkingStrategy` 只依赖 ports/engines，core-only 可导入。
- [ ] Pipeline 与任务入库共享同一 ChunkDraft → Chunk/VectorRecord 映射规则。
- [x] 自定义旧 ChunkHook 有测试覆盖的兼容路径。
- [ ] 语义分块的外部模型调用不进入 engines。

### S2 — 分块正确性

- [ ] 所有策略满足大小、顺序、非空和无 overlap 可重建不变式。
- [ ] 重复文本、连续分隔符、CJK、Markdown 标题/代码块和超长无分隔文本有独立测试。
- [ ] 字符位置在算法内产生；反向改成 `find()` 时重复文本用例会失败。
- [ ] 实际 overlap 可由输出位置观察，文档明确其 best-effort 语义。

### S3 — 结构感知

- [ ] 标题路径从规范 Markdown 产生并传到 Chunk metadata。
- [ ] PageChunker 只使用 extractor page facts；缺失时行为明确。
- [ ] 文档 metadata、请求 metadata 与系统保留字段有唯一合并优先级。
- [ ] 不把 MinerU 专有字段泄漏到 Chunk 或 Port。

### S4 — 层级与可靠性（通过 D8 决策门后执行）

- [ ] IndexPlan 明确父/子、向量化、回填和相邻关系。
- [ ] InMemory/PostgreSQL DocumentStore 通过同一契约。
- [ ] 双存储故障矩阵证明旧 active revision 在失败时仍可检索。
- [ ] 新 revision 激活后旧版本可回收，重试不制造重复可见块。

### S5 — 质量与出口

- [ ] 固定 DOCX/PDF/Markdown 样本比较字符递归、标题和页面策略。
- [ ] 记录块数、长度分布、边界完整性、检索 hit@k、P50/P95 与索引增量。
- [ ] 评测只报告数据，不用单个样本宣称普遍提升。
- [ ] unit < 10s；core-only、integration、e2e、Ruff、Pyright 全绿。

## 7. 实施边界

M4 前半段（T1～T5）不新增第三方依赖、不改数据库或 Milvus schema，可直接按本规格推进。
M4 后半段（T6～T10）必须先提交 schema/迁移/回滚设计并获得确认。任何需要修改
`TaskStore`、`TaskExecutor` 或 `BaseVectorStore` 方法签名的方案都必须另行评审；优先通过
新的窄 Port 与 Service 编排完成。

## 8. 参考实现

- [LangChain text-splitters（评审指定快照）](https://github.com/langchain-ai/langchain/tree/348c9dc572599947d2d7d33d6a5b8b936e92a1d4/libs/text-splitters)
- [LangChain RecursiveCharacterTextSplitter](https://docs.langchain.com/oss/python/integrations/splitters/recursive_text_splitter)
- [LangChain text splitter API](https://reference.langchain.com/python/langchain-text-splitters/langchain_text_splitters)
- [LlamaIndex node_parser](https://github.com/run-llama/llama_index/tree/main/llama-index-core/llama_index/core/node_parser)
- [LlamaIndex HierarchicalNodeParser](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/node_parser/relational/hierarchical.py)
- [LlamaIndex SemanticSplitterNodeParser](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/node_parser/text/semantic_splitter.py)
- [Haystack DocumentSplitter](https://github.com/deepset-ai/haystack/blob/main/haystack/components/preprocessors/document_splitter.py)
- [Haystack HierarchicalDocumentSplitter](https://github.com/deepset-ai/haystack/blob/main/haystack/components/preprocessors/hierarchical_document_splitter.py)
- [RAGFlow chunking guide](https://github.com/infiniflow/ragflow/blob/main/docs/guides/agent/ingestion_pipeline/configure_chunker_component.md)
- [RAGFlow TokenChunker](https://github.com/infiniflow/ragflow/blob/main/rag/flow/chunker/token_chunker.py)

## 9. M4-T1 基线记录

- 代码基线：PR #56 合并提交 `75d1248`，分支 `feature/m4-chunking` 从该提交创建。
- 默认单测：`1856 passed, 20 skipped, 195 deselected, 1 xfailed`，pytest 9.36s。
- 本地静态质量门：Ruff 通过；Pyright 为 `0 errors, 0 warnings`。
- PR #56 CI 的 core-only、unit、Ruff 与 Pyright 全部通过；M4-T2 之后仍需在新改动上重跑。
- 调研日期：2026-09-14。除评审指定的 LangChain 快照外，其他链接指向上游 main，实施
  时只吸收已写入 D1～D10 的设计，不跟随上游无审查漂移。

## 10. M4-T2 验证记录

- 新增 frozen/slots `ChunkDraft`：空白、负 ordinal、半缺失/倒置 span 和非法 metadata
  键在边界处拒绝；metadata 会复制并包装为只读视图。
- 新增 runtime-checkable `ChunkingStrategy`，只有同步 `split(NormalizedDocument)`；模块只
  依赖 engines 与 ports。
- metadata 合并规则落为纯函数，测试覆盖四层优先级、保留键过滤及输入不变性。
- 新增 `PipelineHooks.document_chunker`；旧 `ChunkHook` 通过显式适配器转成 ChunkDraft，
  重复文本的位置保持 `None`，不使用 `find()` 猜测。
- 仅核心依赖的隔离环境可导入 `ChunkDraft`、`ChunkingStrategy` 和旧 Hook 适配器。
- 定向契约/Hook 测试 38 项通过；分层守卫与契约合计 458 项通过；全量单测为
  `1888 passed, 20 skipped, 195 deselected, 1 xfailed`，pytest 9.12s。
- Ruff 与 Pyright 通过，Pyright 为 `0 errors, 0 warnings`。
