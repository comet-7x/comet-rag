# 版本说明

## Unreleased — M3 混合检索

M3 新增 Milvus BM25 关键词召回、RRF 混合召回与单通道降级。以下两项属于部署兼容性
变更，升级前必须处理。

### Python 导入路径收敛

项目仍处于 `0.1.0`，目录归一化分支尚未发布，因此本次直接删除只做转发的历史内部
目录。Loader 与 Pipeline 的稳定入口分别是 `comet_rag.loaders` 和
`comet_rag.pipeline`；具体实现只存在于 `infrastructure/sources`、
`infrastructure/persistence`、`infrastructure/models`、`infrastructure/extractors`
及 `engines/documents` 等规范目录。HTTP DTO 位于 `comet_rag.api.schemas`。

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
