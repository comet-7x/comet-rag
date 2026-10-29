# Comet-RAG

Comet-RAG V2 是可独立组合的文档入库与检索组件库，提供 `AsyncIngestionPipeline` 和 `AsyncSearchPipeline`。已实现本地/HTTP/S3 来源、原生 DOCX 与图片解析、范围切分、实际表示、嵌入/重排、PostgreSQL 内容清单和 Milvus dense/BM25。

真实 CLIP→PostgreSQL/Milvus 已完成文本 dense/keyword/hybrid、文本/图片四种检索组合和 HTTP 临时下载释放。`62e3dd4` 已完成新 scope 入库、PostgreSQL/Milvus/etcd 实际重启后的原 scope 新进程读回，以及远端单元/后端集成/核心 wheel CI。公开 AWS S3 匿名只读与 MinerU 4.0.12 Flash 原生文字 PDF 的真实 HTTP 解析验收通过；MinerU 仍为 Markdown-only partial，扫描 OCR、其它 tier、兼容 S3、认证与版本对象行为未验证。具体证据和剩余出口见 [验证记录](docs/validation.md)。

旧接口、HTTP 服务、worker 和运行说明保留在 [refactor/enterprise-reliability](https://github.com/comet-7x/comet-rag/tree/refactor/enterprise-reliability)。V2 不提供旧接口兼容、回答生成、在线版本激活或后台 GC。

## 安装与组件

Python >=3.12。核心依赖只有 lxml；后端和模型 SDK 按实际用途安装：

```bash
pip install comet-rag
pip install 'comet-rag[postgres,milvus,local-models,images,sources]'
```

当前分支版本为 `0.2.0.dev0`，未发布到包索引；以上为包安装形式，本地开发使用下方 `make install`，本地 wheel 可用 `pip install 'dist/comet_rag-0.2.0.dev0-py3-none-any.whl[postgres,milvus,local-models,images,sources]'`。

| 组件 | 导入位置 |
|---|---|
| 内容、媒体引用与身份 | `content`、`identity` |
| 单资源获取与路由 | `sources`；HTTP 适配器 `sources.http` |
| 文档结构、投影、编解码、Markdown 导出 | `documents.schemas`、`documents.projection`、`documents.codec`、`documents.exporters` |
| 原生文本与 DOCX | `documents.parsers` |
| 独立图片 / 外部 PDF | `documents.parsers.image` / `documents.parsers.mineru` |
| 范围切分 | `chunkers` |
| Flat / ParentChild 及实际表示 | `indexing` |
| 嵌入、重排数据契约 | `embedder`、`rerankers` |
| 模型适配器 | `embedder.models.clip`、`embedder.models.openai`、`rerankers.models.local` |
| 内容 / 向量存储 | `docstores` / `vectorstores`，持久 SDK 从 `.postgres` / `.milvus` 显式导入 |
| 入库 / 搜索 | `pipelines` |

组件按包根 `comet_rag` 导入。核心导入不加载 torch、httpx、数据库 SDK 或服务运行时。`AsyncOpenAITextEmbedder` 提供有预算/映射校验的文本协议适配，尚无远端真实模型验收；实际模型验证使用本地固定 CLIP/MiniLM。

```python
from pathlib import Path

from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker
from comet_rag.documents.parsers import AsyncDocxParser
from comet_rag.indexing import FlatIndexBuilder
from comet_rag.pipelines import AsyncIngestionPipeline, AsyncSearchPipeline
from comet_rag.sources import AsyncLocalSourceResolver, LocalSource

# embedder、docstore、vectorstore 由调用方创建并绑定同一 scope/profile/space。
def ingestion(embedder, docstore, vectorstore):
    builder = FlatIndexBuilder(DocumentChunker(RecursiveTextChunker(1000)))
    return AsyncIngestionPipeline(
        resolver=AsyncLocalSourceResolver(),
        parser=AsyncDocxParser(),
        builder=builder,
        embedder=embedder,
        docstore=docstore,
        vectorstore=vectorstore,
        output_dir=Path("retained-assets"),
    )

async def keyword_search(docstore, vectorstore):
    search = AsyncSearchPipeline(docstore=docstore, vectorstore=vectorstore)
    return await search.run("检索词", mode="keyword", top_k=5)
```

投影 `project_document` 和 Markdown 导出均在展开前检查全篇累计表格网格预算，默认 `max_table_cells=10_000`（零列表格按行计）；调用方可通过同名关键字显式调整。`DocumentChunker` 使用该默认上限，并提前拒绝仅合成分隔符就超过 chunk 预算的表格。Flat 构建配置已升级为 `flat_v4`，表格图片按行列及格内块顺序物化，重复出现仍保留；旧 profile 在新 scope 重建后切换。

注入客户端由创建者关闭。blocks 是主数据，Markdown 通过 `from comet_rag.documents.exporters import export_markdown` 同步派生；文本投影的范围以 Python 字符 `[start,end)` 表示。导出保留表格跨度和原生公式源码，显示效果由调用方渲染器决定。`partial` 解析默认不入库。资源在下载释放前转存稳定目录，目录持久化与跨机器共享由调用方负责。

同一 scope/source 仅允许相同快照与配置重试，清单保存首次实际向量以供精确重放。资料或策略变化使用新 `index_scope` 重建、核对完整 manifest 和 record 指纹，再由调用方切换实例。清单格式 v2 使用默认 PostgreSQL schema `comet_docstore_v2`。DocStore 单元与表示的 JSON 存储封套另带 `snapshot_version=1` 和独立摘要，读取时核验完整快照；公共索引 codec 不变。缺少摘要的旧 scope 必须重建后切换，不自动补摘要或迁移；同一 scope 不混用新旧 writer。PostgreSQL 内部事务不等于跨存储原子提交；写向量超时保留 unknown 清单。删除按清单明确 IDs，不自动清理共享 Asset。

## 异常

`from comet_rag import CometRAGError` 可统一捕获库定义的异常。现有 `SourceError`（包括 `SourceResourceLimitExceeded`）、`ArchiveResourceLimitExceeded` 和 `IngestionIncomplete` 均继承该基类，同时保留原有的 `ValueError` 或 `RuntimeError` 分类；模块专属信息仍由具体异常携带。

这不是所有失败的包装：参数校验仍可能抛出 `ValueError`／`TypeError`，文件系统和 SDK 异常按组件契约传播，`asyncio.CancelledError` 始终传播。后续模块新增自定义异常时继承 `CometRAGError`，不增加错误码或统一响应结构。

## 可运行示例与检查

[examples](examples/README.md) 提供实际运行的持久文本/DOCX 和 HTTP 图片/文档图片示例，包含新进程 verify。父子上下文、重排与表示策略的真实质量/成本比较见 [evals](evals/README.md)。当前默认 flat、不启用重排；CLIP 不支持联合图文，不能用向量平均代替。

```bash
make install
make check
make coverage
make pre-commit
```

`make check` 执行 Ruff、Pyright、无真实服务的单元测试和 wheel/sdist 构建。真实集成显式选择；缺少后端直接失败，不把 skip 当作验收通过。仅核心 wheel 在仓库外、无开发依赖环境执行跨进程读回，CI 使用相同脚本。

开发 Compose 提供回环端口上的 PostgreSQL/Milvus：`make db-up`、`make db-down`，后者保留数据卷。[开发环境](docs/development.md)说明配置和所有权；[测试说明](tests/README.md)列出严格集成入口。

## 阶段状态

| 阶段 | 当前交付与验收 |
|---|---|
| 0 / A | 清场、独立包、身份/文档契约与隔离环境已提交 |
| B | 持久文本/DOCX、三模式及最新代码实际服务重启后的原 scope 读回通过，见 [B](docs/phase-b.md) |
| C | 本地/HTTP/原生 DOCX/公开 AWS S3 只读与 MinerU 4.0.12 Flash 文字 PDF 已验证；扫描 OCR、其它 tier 与兼容 S3 等未验证，见 [C](docs/phase-c.md) |
| D | 四种组合、持久图片及最新代码实际服务重启后的原 scope 读回通过，见 [D](docs/phase-d.md) |
| E | 可选父子上下文/重排与实际模型比较，见 [E](docs/phase-e.md) |
| F | 组件导出、示例/评估、包与 CI 配置已交付；核心及来源/图片 wheel 外部调用通过，阶段 C 的首版 MinerU 出口已补齐；未验证能力见验证记录 |

职责和不变量见 [架构](docs/architecture.md)，注释约定见 [注释标准](docs/comment_style.md)。本地细化设计为 `tmp/refactoring/overview.md`（v1.3），不随分发包发布。
