# Comet-RAG

Comet-RAG V2 正在整体重写为可独立组合的文档入库与检索组件库。当前完成阶段 0：清除旧体系并建立可安装、可测试的开发骨架。

当前可用的是 ZIP/XML 资源预算检查和单元排名 RRF。文档解析、来源获取、模型、持久存储、`AsyncIngestionPipeline` 与 `AsyncSearchPipeline` 尚未实现，当前版本不能完成 RAG 入库或搜索。

旧接口、服务和使用说明保留在 [refactor/enterprise-reliability](https://github.com/comet-7x/comet-rag/tree/refactor/enterprise-reliability)。V2 不提供旧接口兼容。

## 开发与检查

需要 Python >=3.12 和 uv，在当前 checkout 中运行：

```bash
make install
make check
make coverage
```

`make check` 执行 Ruff、Pyright、单元测试和包构建。`make install` 只安装锁定的开发依赖；需要提交钩子时执行 `uv run --locked pre-commit install`。

核心安装仅包含当前实现所需的 lxml；python-docx 用于开发阶段生成来源夹具。PostgreSQL、Milvus、S3 和模型 SDK 的依赖组随实际适配器加入。

```python
from comet_rag.documents.parsers import ArchiveLimits, validate_zip_archive
from comet_rag.retrieval import reciprocal_rank_fusion

validate_zip_archive("sample.docx", ArchiveLimits(max_members=1_000))

# 输入为各通道已经按相关性排列的 unit_id，原始分数不参与融合。
fused = reciprocal_rank_fusion(
    {"dense": ["unit-a", "unit-b"], "keyword": ["unit-b"]}
)
assert fused[0].unit_id == "unit-b"
```

容器开发配置只提供独立的 V2 PostgreSQL 和 Milvus 及其依赖：

```bash
make db-up
make db-down
```

这些命令由开发者显式运行。配置使用独立项目、数据卷和回环端口；`db-down` 保留数据。阶段 0 不连接或迁移数据库，后端准备说明见 [开发环境](docs/development.md)。

## 实施路线

| 阶段 | 交付 |
|---|---|
| 0（已完成） | 清场、独立包、开发工具与基础行为验证 |
| A | 身份/编码/投影契约、保护行为归属、真实样例与后端配置 |
| B | 本地文本与基础 DOCX，PostgreSQL/Milvus 持久入库，dense/keyword/hybrid |
| C | HTTP/S3、DOCX 保真、外部 MinerU PDF 与持久资源 |
| D | 独立图片与文档图片，真实文本/图片四种检索组合 |
| E | 可选父子上下文、重排和表示策略比较 |
| F | 完整组件导出、实际运行示例与集成交付 |

阶段 0 的实际改动与核验见 [验收记录](docs/phase0.md)。目标职责与约束见 [架构](docs/architecture.md)，验证范围见 [测试说明](tests/README.md)，代码注释约定见 [注释标准](docs/comment_style.md)。本次本地详细设计入口为 `tmp/refactoring/overview.md`（v1.3），该目录保持忽略，不随包分发。
