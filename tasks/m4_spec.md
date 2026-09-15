# Spec: M4 Chunking 与层级索引

> 状态：已冻结，M4-T5 已完成（v1.6）
> GitHub Issue：[#57](https://github.com/comet-7x/comet-rag/issues/57)
> 开发分支：`feature/m4-chunking`
> 最后更新：2026-09-15

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

### D1 — 原子 Chunker 使用最小输入，Strategy 编排完整文档

```python
class Chunker(Protocol):
    def split(self, text: str, /) -> list[ChunkDraft]: ...


class ChunkingStrategy[ResultT](Protocol):
    def split(self, document: NormalizedDocument, /) -> ResultT: ...
```

- 原子 `Chunker` 只消费算法真正需要的 `str`，可以脱离 Pipeline 单独使用；
- `ChunkDraft.start_char/end_char` 始终相对于传入字符串，Service 传入
  `document.markdown` 时也就等于规范 Markdown 的字符位置；
- 文档级 `ChunkingStrategy` 才消费 `NormalizedDocument`，并基于一个或多个 Chunker
  编排标题、页面或父子块；结果类型泛型化，平坦策略可返回 `list[ChunkDraft]`，未来
  父子策略可以返回纯计算的 `ChunkHierarchy`，不把层级压扁进 metadata；
- 两者都不生成依赖 `source_id` 的最终 ID，不持有 embedding，也不做 I/O；
- 两者只有同步方法。服务的 async 路径统一使用一次 `asyncio.to_thread()`。

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

当前真正不同的原子算法只有两种，后续只有算法过程不同时才增加新的 Chunker：

- `FixedSizeChunker`：无自然边界时的确定性兜底；
- `RecursiveChunker`：按 separator profile 递归切分；

文本、DOCX、Markdown、CSV、JSON、XML 和代码语言之间的差异只是
`chunk_size`、`chunk_overlap`、separators 与 separator 归属，应使用不可变
`ChunkProfile` 表达，不能继续创建参数型子类。

文件类型选择属于 Service/Hook 路由；separator profile 是纯数据，不能继续用大量几乎
相同的子类表达配置差异。

T4.2 将所有 separator 常量收敛为元组，防止调用方修改全局列表后改变后续分块行为。
`engines/chunkers/types.py` 只拥有 `ChunkDraft`；来源、知识库、revision、parent 等
metadata 保留键及其合并优先级属于物化用例，统一由 `services/chunking.py` 管理。

仓库版本仍为 0.1.0，且没有 tag/Release。T4.1 已删除 `BaseChunker`、
`RecursiveCharacterTextSplitter`、所有格式参数子类及 `CodeRecursiveChunker`；调用方使用
`RecursiveChunker.from_profile(code_profile("rust"))`，运行时仍只有递归算法本身。

### D5 — 结构事实使用引用规范 Markdown 的 DocumentBlock

M4 需要标题和页面边界时，在 `ports/document.py` 增加最小 `DocumentBlock`。它用
`start_char/end_char` 引用 `NormalizedDocument.markdown`，并只记录真实消费者需要的
`kind`、`ordinal`、可选 `page_number`、`heading_path` 和 metadata。正文只保存一份，
避免 `markdown` 与 block.text 成为两套真相。

- 标题结构可由 normalization 后的 Markdown 分析器产生；
- 页码必须来自 DOCX/PDF Extractor 的事实；没有页信息时 PageChunkingStrategy 明确拒绝或由
  Planner 选择递归策略，不把换行猜成分页；
- bbox、图片资产和表格单元格坐标不在首版字段中，出现真实消费用例后再扩展。

PDF 页事实采用“双重校验”接入：MinerU 继续以 `md_content` 为正文真相，同时请求 legacy
`content_list`。适配器只对能够无损重建的文本、标题、公式和代码项按 `page_idx` 分组；
重建结果必须与 `md_content` 完全一致。Normalizer 再对逐页片段和全文分别规范化，只有
逐页重建仍等于规范全文时才按游标直接生成 page span。任一条件不满足即回退 Markdown
section，不使用 `find()`，也不向 Port/Chunk 暴露 `page_idx` 等 MinerU 专有字段。

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

### D10 — Hook 兼容与算法兼容分开处理

- 新增文档级 Hook 时为 `PipelineHooks.chunker` 提供明确适配器，现有自定义 hook 不立即失效；
- `Pipeline` 与任务入库必须最终调用同一个 Chunking Service，不能各自复制 metadata
  合并与 ID 生成；
- 旧 Hook 标注弃用版本和移除窗口，但仓库尚未发布的重复算法和参数型类直接删除，
  M4 不保留两份核心算法。

具体迁移入口是 `PipelineHooks.document_chunker`；旧 `PipelineHooks.chunker` 在 T4 新链路
启用后开始发出弃用提示，继续支持整个 0.2.x，最早在 0.3.0 移除。内置 Hook 必须先迁到
新契约，不能让框架自身触发弃用提示。

T4 已将两条入口收敛到 `ChunkingService`：Pipeline 与 IngestRunner 都消费完整
`NormalizedDocument`，再使用同一物化函数生成 ID、位置和 metadata。跨 worker 保存
经过严格字段校验、JSON 序列化校验及字节上限保护的 DocumentBlock 与 ChunkDraft 窄
payload，不持久化 dataclass 或只读映射对象。这样任务链路不会在 extracting → chunking
之间丢失标题或页面事实。

## 6. 成功标准

### S1 — 契约与分层

- [x] `Chunker` 与 `ChunkingStrategy` 只依赖 ports/engines，core-only 可导入。
- [x] Pipeline 与任务入库共享同一 ChunkDraft → Chunk/VectorRecord 映射规则。
- [x] 自定义旧 ChunkHook 有测试覆盖的兼容路径。
- [ ] 语义分块的外部模型调用不进入 engines。

### S2 — 分块正确性

- [x] Fixed/Recursive 满足大小、顺序、非空和无 overlap 可重建不变式。
- [x] 重复文本、连续分隔符、CJK、代码前缀和超长无分隔文本有独立测试。
- [x] 字符位置在算法内产生；重复文本测试会阻止事后 `find()` 回查。
- [x] 实际 overlap 可由输出位置观察，文档明确其 best-effort 语义。

### S3 — 结构感知

- [x] 标题路径从规范 Markdown 产生并传到 Chunk metadata。
- [x] PageChunkingStrategy 只使用真实 page facts；缺失时行为明确。
- [x] 文档 metadata、请求 metadata 与系统保留字段有唯一合并优先级。
- [x] 不把 MinerU 专有字段泄漏到 Chunk 或 Port。

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

- 新增 frozen/slots `ChunkDraft`：空字符串、负 ordinal、半缺失/倒置 span 和非法 metadata
  键在边界处拒绝；metadata 会复制并包装为只读视图。旧 Hook 适配器进一步
  拒绝空白块；新策略只在保全非空原文的连续空白时允许空白 span。
- 新增 runtime-checkable `ChunkingStrategy`，只有同步 `split(NormalizedDocument)`；模块只
  依赖 engines 与 ports。
- metadata 合并规则落为纯函数，测试覆盖四层优先级、保留键过滤及输入不变性。
- 新增 `PipelineHooks.document_chunker`；旧 `ChunkHook` 通过显式适配器转成 ChunkDraft，
  重复文本的位置保持 `None`，不使用 `find()` 猜测。
- 仅核心依赖的隔离环境可导入 `ChunkDraft`、`ChunkingStrategy` 和旧 Hook 适配器。
- 定向契约/Hook 测试 38 项通过；分层守卫与契约合计 458 项通过；全量单测为
  `1888 passed, 20 skipped, 195 deselected, 1 xfailed`，pytest 9.12s。
- Ruff 与 Pyright 通过，Pyright 为 `0 errors, 0 warnings`。

## 11. M4-T3 验证记录

- 新增 `FixedSizeChunker` 与 `RecursiveChunker`；split、recursive merge 与硬切兜底
  全程传递字符 span，输出可直接回引规范 Markdown。
- 新增可注入 `LengthFunction`；默认 `len` 按 Unicode code point 计量，字节
  计数、非法计数器和单字符超预算均有独立测试。
- T3 首先用单一代码类消除了按语言复制算法；T4.1 进一步确认它仍只是参数门面，
  最终收敛为 `code_profile(...) + RecursiveChunker`，`.rs` 正确归一为 Rust。
- 反向注入“事后从头 `find()` 位置回查”和“丢弃 separator”两类缺陷，
  重复文本位置断言与无 overlap 逐字重建断言分别失败；恢复后重跑通过。
- 新旧 Chunker 定向测试 `197 passed`；全量单测
  `1958 passed, 20 skipped, 195 deselected, 1 xfailed`，pytest 9.05s。
- core-only 临时环境可导入并运行三个新策略，未加载 infrastructure；
  `make lint` 通过，Pyright 为 `0 errors, 0 warnings`。

## 12. M4-T4 验证记录

- 新增共享 `ChunkingService`，Pipeline 的同步、异步、流式入口与 IngestRunner 均通过
  文档级 Hook 产生 `ChunkDraft`，不再维护两套分块与物化规则。
- ID、document/request/chunk/system metadata 和字符位置由共享物化函数统一生成；同源
  文档在 Pipeline 与任务入库链路中的 ID、正文和 metadata 已有交叉测试。
- Task context 使用窄 JSON payload 移交 ChunkDraft，并拒绝未知字段、隐式布尔整数、
  不可序列化 metadata 与超出配置字节上限的载荷。
- 反向同时移除载荷与值对象的布尔整数守卫、移除未知字段守卫，对应测试均会失败；
  恢复实现后定向测试重新通过。
- 框架内置 Hook 已迁到 `document_chunker`；旧 `chunker(str, config)` 仍可工作，调用时
  发出带 0.3.0 移除窗口的 `DeprecationWarning`。
- 仓库没有 Tag、Release 或旧语言子类的运行时调用方，因此 T4 删除 `PythonChunker`、
  `RustChunker` 等临时公开面；T4.1 又删除仅包装画像的 `CodeRecursiveChunker`。
- 全量单测为 `1950 passed, 20 skipped, 195 deselected, 1 xfailed`，pytest 9.26s；
  E2E `30 passed`；integration `6 passed, 152 skipped`；benchmark `7 passed`。
- core-only 隔离环境可运行 `RecursiveChunker` 与 `ChunkingService`，未加载
  infrastructure；Ruff 与 Pyright 全绿，Pyright 为 `0 errors, 0 warnings`。

## 13. M4-T4.1 验证记录

- 新增原子 `Chunker` Protocol：`FixedSizeChunker`、`RecursiveChunker` 直接消费 `str`，
  `ChunkDraft` span 相对于该字符串；文档级 `ChunkingStrategy` 才消费完整规范文档。
- 删除 `BaseChunker`、`RecursiveCharacterTextSplitter` 以及所有只改参数的格式/代码类；
  六种格式与 11 种代码语言改由不可变 `ChunkProfile` 表达。
- 旧实现与新实现的差分审计证明二者并不等价：separator 后置、连续 separator 和无匹配
  separator 的 overlap 存在边界差异，因此没有建立错误的“新旧输出恒等”测试。
- 新增 separator start/end 确切输出、非法值、首尾/连续分隔符、固定兜底 overlap、
  profile 默认值及直接字符串调用测试；反向移除非法值检查和禁用 start 分支时，对应
  测试均明确失败，恢复后重新通过。
- 全量单测为 `1908 passed, 20 skipped, 195 deselected, 1 xfailed`，pytest 9.48s；
  E2E `30 passed`；integration `6 passed, 152 skipped`；benchmark `7 passed`。
- core-only 隔离环境可直接构造两种原子 Chunker、应用代码 profile 且未加载
  infrastructure；Ruff 与 Pyright 全绿，Pyright 为 `0 errors, 0 warnings`。

## 14. M4-T4.2 验证记录

- separator 常量全部改为不可变元组，并移除 profile 构造时多余的重复复制。
- `engines/chunkers/types.py` 只保留 `ChunkDraft`；metadata 优先级和来源、知识库、
  revision、parent 等保留键归还 `services/chunking.py`。
- CI 的 core-only 冒烟从已删除的 `TextChunker` 迁到 `RecursiveChunker`。
- 将任一 separator 临时恢复为列表时，不可变性测试明确失败；恢复后全量单测为
  `1908 passed, 20 skipped, 195 deselected, 1 xfailed`，pytest 9.52s；Ruff、Pyright 与
  core-only 冒烟通过。

## 15. M4-T5.1～T5.3 验证记录

- `DocumentBlock` 用不可变字符 span 表达 section/page 结构，不复制正文；
  `NormalizedDocument` 校验 block 类型、连续 ordinal、边界、顺序和不重叠。
- `MarkdownStructureAnalyzer` 支持 ATX/Setext 标题，维护标题路径，并忽略反引号或波浪线
  fenced code 内的伪标题；规范化阶段直接生成 section blocks。
- `MarkdownSectionStrategy` 与 `PageChunkingStrategy` 都在结构硬边界内调用注入的原子
  Chunker，把局部 span 转成整篇文档 span，并生成 JSON 稳定的标题路径或页码事实。
- 默认 Hook 按 page/section kind 选择策略，无结构时才回退 `RecursiveChunker`；未知混合
  kind 和缺失页码会明确报错，不猜测。
- 反向禁用 fence 状态、移除局部到全局 span 偏移时，对应标题和页面性质测试均明确失败。
- 全量单测 `1990 passed, 20 skipped, 195 deselected, 1 xfailed`，pytest 9.36s；E2E
  `30 passed`，integration `6 passed, 152 skipped`，benchmark `7 passed`；Ruff、Pyright
  和结构分块 core-only 冒烟全部通过。

## 16. M4-T5.4～T5.5 验证记录

- 新增通用 `ExtractedPage`，只表达从 1 开始的页号、页 Markdown 和可选通用 metadata；
  MinerU 的 0-based `page_idx` 在适配器边界完成转换。
- MinerU 明确请求 `content_list`，但 `md_content` 仍是唯一正文真相。legacy content list
  只有在支持项可逐页无损重建且全文完全一致时才产生页事实；图片、表格、图表、列表、
  未知类型、乱序页号和内容不一致均安全回退 section。
- Normalizer 对全文与每页独立执行同一规范化，再通过顺序游标构造 page span；重复页文本
  用例证明实现不依赖文本搜索。
- DocumentBlock 以窄 JSON payload 跨 Task 阶段传递，未知字段、错误类型、非 JSON metadata
  和字节上限均在服务边界拒绝；chunking 后结构与全文一起清理。
- 反向禁用官方 content list 字符串解析、丢弃 extracting 阶段的 DocumentBlock payload，
  对应 MinerU 页事实与跨阶段用例均明确失败；恢复后重新通过。
- 固定 Markdown、合成 DOCX 与 MinerU PDF 样本覆盖标题/页面硬边界、metadata 传播及
  extracting → chunking 交接。
- 全量单测 `2008 passed, 20 skipped, 195 deselected, 1 xfailed`，pytest 9.49s；E2E
  `30 passed`，integration `6 passed, 152 skipped`，benchmark `7 passed`；Ruff、Pyright
  与隔离 core-only 页结构冒烟全部通过。
