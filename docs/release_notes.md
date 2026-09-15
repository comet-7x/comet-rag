# 版本说明

## Unreleased — 目录与文档处理边界重构

本轮在已完成的 M3 混合检索基础上收敛目录，并为 M4 冻结统一文档输入。以下变更
包含 Python API 与部署兼容性调整，升级前必须处理。

### Python 导入路径收敛

项目仍处于 `0.1.0`，目录归一化分支尚未发布，因此本次直接删除只做转发的历史内部
目录。Loader 与 Pipeline 的稳定入口分别是 `comet_rag.loaders` 和
`comet_rag.pipeline`；具体实现只存在于 `infrastructure/sources`、
`infrastructure/persistence`、`infrastructure/models`、`infrastructure/extractors`
及 `engines/documents` 等规范目录。HTTP DTO 位于 `comet_rag.api.schemas`。

### 文档 Hook 返回值变更

`PipelineHooks.extractor()` 与 `aextractor()` 注册的函数现在必须返回
`ExtractedDocument`，不再直接返回字符串。Pipeline 会统一转换为
`NormalizedDocument` 后再交给文档级 Chunking Strategy；Strategy 把 Markdown 交给
原子 Chunker，避免 DOCX、MinerU 与后续 PDF/OCR 各自维护一套跨格式空白和编码规则。
自定义 Hook 可按以下方式迁移：

```python
return ExtractedDocument(markdown=text, metadata={"provider": "custom"})
```

### Chunker API 收敛

原子分块器现在直接接收 `str`，当前只有 `FixedSizeChunker` 和 `RecursiveChunker`。
`BaseChunker`、`RecursiveCharacterTextSplitter` 及按格式/代码语言命名的参数型类已经
删除；对应差异改用 `ChunkProfile`、`language_profile()` 与 `code_profile()` 表达。
所有 separator 常量均为不可变元组；来源、知识库与索引关系的 metadata 合并规则位于
`services.chunking`，不再作为原子 Chunker 的公共 API。

`MarkdownDocumentNormalizer` 现在生成引用规范 Markdown 的 section blocks；内置
`MarkdownSectionStrategy` 保留标题路径，`PageChunkingStrategy` 只接受带真实页码的
page blocks。两个策略都复用原子 Chunker，输出位置仍指向整篇规范 Markdown。

MinerU 现在可从经过全文一致性校验的 legacy content list 生成页事实；无法无损重建时
保守回退 section。DocumentBlock 同时通过严格 JSON payload 跨 Task 阶段保存，不再因
worker 交接丢失页面或标题结构。

### 配置变更

`infrastructure_config.vector_database.collection_name` 已移除，改为：

```yaml
infrastructure_config:
  vector_database:
    endpoint: http://localhost:19530
    database_name: your_database       # 必填，不会回落到 default
    collection_prefix: comet
    replica_number: 1
```

应用与集成测试必须使用明确隔离的 database。测试代码只允许访问
`zhihao_test_database`，且不会自行创建或删除 database。

### Milvus schema v2 是破坏性变更

M1/M2 创建的旧 collection 没有 chinese analyzer、BM25 function 或 sparse index，
不能原地变成 M3 schema v2。所有读写入口都会校验 schema：检索请求返回 HTTP 409，
入库任务在 indexing 阶段永久失败。服务不会自动删除、迁移或覆盖已有 collection。

升级步骤：

1. 备份或导出需要保留的原始文档与 collection 数据。
2. 在配置中显式填写目标 `database_name` 和 `collection_prefix`，确认没有指向生产外的库。
3. 停止写入；通过 `collection_name_for(kb_id, prefix=...)` 核对每个知识库对应的
   collection 名。
4. 仅在确认原始数据可重建后，由操作者显式删除旧 collection。
5. 重新提交全部数据源，使用当前 embedding 模型整库向量化；不要把新旧模型的向量
   混在一个知识库中。
6. 验证入库任务成功，再分别执行 dense、keyword、hybrid 查询后恢复流量。

建议先在一个可重建的知识库上做 canary。没有原始数据或备份时不要删除 collection。
