# Pipeline 使用笔记

本文档记录公共入口 `comet_rag.pipeline` 的 `Pipeline` / `AsyncPipeline` 及其配置的用法，涵盖基本使用、流式输出、批量处理、自定义 Hook 扩展，以及底层模块的独立使用方式。DOCX 是纯库内置能力；PDF 需要外部 MinerU，并由服务组合根或库调用方显式注册。

---

## 目录

1. [快速开始](#1-快速开始)
2. [PipelineConfig 配置项](#2-pipelineconfig-配置项)
3. [流式输出](#3-流式输出)
4. [批量处理](#4-批量处理)
5. [输出数据结构](#5-输出数据结构)
6. [自定义 Hook 扩展新格式](#6-自定义-hook-扩展新格式)
7. [覆盖内置 Hook](#7-覆盖内置-hook)
8. [底层模块独立使用](#8-底层模块独立使用)
9. [目前支持的文件格式](#9-目前支持的文件格式)
10. [PDF 与外部 MinerU](#10-pdf-与外部-mineru)

---

## 1. 快速开始

```python
from comet_rag.pipeline import Pipeline

# 使用默认配置（chunk_size=2000, chunk_overlap=200）
with Pipeline() as pipeline:
    result = pipeline.run("path/to/document.docx")

print(f"共 {len(result.chunks)} 个 chunk")
for chunk in result.chunks:
    print(chunk.text[:100])
```

异步版本使用独立的 `AsyncPipeline`，方法仍叫 `run`。旧 `arun`、`astream_run`、
`abatch_run` 已移除。默认 Loader 由 Pipeline 创建，`with` / `async with` 或相应的
`close()` 负责释放；外部注入的 Loader、模型仍由调用方管理。

异步示例：

```python
import asyncio
from comet_rag.pipeline import AsyncPipeline


async def main():
    async with AsyncPipeline() as pipeline:
        result = await pipeline.run("path/to/document.docx")
        print(len(result.chunks))


asyncio.run(main())
```

---

## 2. PipelineConfig 配置项

```python
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.pipeline import Pipeline

config = PipelineConfig(
    chunk_size=1500,  # 每个 chunk 的最大字符数，默认 2000
    chunk_overlap=150,  # 相邻 chunk 的重叠字符数，默认 200
    embed=False,  # 是否自动对 chunk 调用 embedding 模型，默认 False
    max_concurrency=8,  # batch 模式的最大并发数，默认 8
)

pipeline = Pipeline(config=config)
```

格式专属配置放在子配置对象里，会被传递给对应的 hook：

```python
from comet_rag.engines.pipelines import DocxConfig, PipelineConfig

config = PipelineConfig(
    chunk_size=1500,
    docx=DocxConfig(
        heading_numbers=False,  # 标题是否保留编号，默认 False
        include_images=True,  # 是否保留图片，默认 True
        include_headers_footers=False,  # 是否保留页眉页脚，默认 False
        vision_model=None,  # 传入视觉模型则为图片生成描述，默认 None
    ),
)
```

启用 Embedding（需要提供 `embedding_model`）：

```python
from comet_rag.engines.pipelines import PipelineConfig
from comet_rag.pipeline import Pipeline
from comet_rag.infrastructure.models.embedding.qwen3_vl import (
    Qwen3VLEmbeddingModel,
)

embedding_model = Qwen3VLEmbeddingModel(
    base_url="http://your-service/v1",
    model_name="Qwen/Qwen3-VL-Embedding-8B",
    api_key="EMPTY",
)

try:
    with Pipeline(
        config=PipelineConfig(embed=True),
        embedding_model=embedding_model,
    ) as pipeline:
        result = pipeline.run("document.docx")
        print(result.chunks[0].embedding)  # list[float]
finally:
    embedding_model.close()
```

---

## 3. 流式输出

适合大文件场景，每个 chunk 处理完立即 yield，无需等待全部完成。

```python
from comet_rag.pipeline import Pipeline

with Pipeline() as pipeline:
    for chunk in pipeline.stream_run("document.docx"):
        print(f"chunk {chunk.metadata['chunk_index']}: {chunk.text[:80]}")
```

```python
import asyncio
from comet_rag.pipeline import AsyncPipeline


async def stream():
    async with AsyncPipeline() as pipeline:
        async for chunk in pipeline.stream_run("document.docx"):
            print(chunk.text[:80])


asyncio.run(stream())
```

> **注意**：`AsyncPipeline.stream_run` 返回的是 `AsyncGenerator`，需要用 `async for` 迭代，不能 `await`。

---

## 4. 批量处理

```python
from comet_rag.pipeline import AsyncPipeline, Pipeline

sources = [
    "docs/report_2024.docx",
    "docs/manual.docx",
    "https://example.com/spec.docx",  # 支持 URL
]

# 同步批量（线程池并发）
with Pipeline() as pipeline:
    results = pipeline.batch_run(sources)

# 在 async def 或支持顶层 await 的环境中执行
async with AsyncPipeline() as async_pipeline:
    async_results = await async_pipeline.batch_run(sources)

for result in results:
    print(
        f"{result.file_type} | {result.source_id[:8]}... | {len(result.chunks)} chunks"
    )
```

---

## 5. 输出数据结构

### `PipelineResult`

```python
@dataclass
class PipelineResult:
    source_id: str  # SHA256(文件绝对路径 或 URL)
    file_type: str  # 文件扩展名，如 "docx"
    chunks: list[Chunk]  # chunk 列表
    metadata: dict[
        str, Any
    ]  # Extractor metadata 与 LoadedResource metadata 的合并结果
```

### `Chunk`

```python
@dataclass
class Chunk:
    id: str  # SHA256(source_id + ":" + chunk_index)
    text: str  # chunk 文本内容
    metadata: dict[str, Any]  # 见下表
    embedding: list[float] | None  # embed=True 时填充，否则 None
```

`Chunk.metadata` 字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `source` | `str` | 原始路径或 URL |
| `source_id` | `str` | SHA256 |
| `file_type` | `str` | 如 `"docx"` |
| `total_chunks` | `int` | 该文件产生的 chunk 总数 |
| `chunk_index` | `int` | 当前 chunk 的序号（从 0 开始） |
| `chunk_start` | `int` | 在规范 Markdown 中的起始字符位置（旧 Hook 可能缺失） |
| `chunk_end` | `int` | 在规范 Markdown 中的结束字符位置（左闭右开） |
| `block_kind` | `str` | 结构策略产生的 `section` 或 `page` |
| `heading_path` | `list[str]` | Markdown 标题路径；无标题的前言为空列表 |
| `page_number` | `int` | 仅在上游提供真实页事实时存在，从 1 开始 |

---

## 6. 自定义 Hook 扩展新格式

`HookRegistry` / `AsyncHookRegistry` 是同步和异步注册表类型；
`PipelineHooks` / `AsyncPipelineHooks` 是各自的默认注册表**实例**，无需也不能再构造。
`Pipeline` 与 `AsyncPipeline` 分别读取对应默认实例。需要局部配置时调用 `.fork()`，
或创建新的空注册表后通过 `hooks=` 注入；注册一个表不会自动修改另一个表。

增加新格式时注册以下 Hook：

- **extractor**：`(LoadedResource, PipelineConfig) → ExtractedDocument`，负责把文件映射为通用提取结果
- **document_chunker**（可选）：`(NormalizedDocument, PipelineConfig) → list[ChunkDraft]`，自定义分块策略；不注册时按 page/section 事实选择内置策略，无结构才回退 `RecursiveChunker`

所有提取结果都会在进入文档级 Strategy 前经过统一的 `MarkdownDocumentNormalizer`；
原子 Chunker 只接收 Strategy 选择出的 Markdown 字符串。

> 两个 hook 都接收完整的 `PipelineConfig`，而不是散装的 `chunk_size` / `chunk_overlap`。
> 这样新增格式专属配置（如 `config.docx`）时无需改动 hook 签名。

```python
from comet_rag.loaders import LoadedResource
from comet_rag.engines.chunkers import ChunkDraft, RecursiveChunker
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks
from comet_rag.ports import ExtractedDocument, NormalizedDocument


# 注册纯文本 extractor
@PipelineHooks.extractor("txt", "log")
def extract_plaintext(
    loader_content: LoadedResource, config: PipelineConfig
) -> ExtractedDocument:
    return ExtractedDocument(
        markdown=loader_content.path.read_text(encoding="utf-8")
    )


# 注册 Markdown extractor
@PipelineHooks.extractor("md", "mdx")
def extract_markdown(
    loader_content: LoadedResource, config: PipelineConfig
) -> ExtractedDocument:
    return ExtractedDocument(
        markdown=loader_content.path.read_text(encoding="utf-8")
    )


# 可选：覆盖默认的结构感知策略
@PipelineHooks.document_chunker("md", "mdx")
def chunk_markdown(
    document: NormalizedDocument, config: PipelineConfig
) -> list[ChunkDraft]:
    return RecursiveChunker(
        config.chunk_size,
        config.chunk_overlap,
    ).split(document.markdown)
```

旧 `PipelineHooks.chunker(str, config) -> list[str]` 仍可运行，但会发出
`DeprecationWarning`，且无法提供可信的字符位置；请在 0.3.0 前迁移到
`document_chunker`。

注册之后，`Pipeline` 无需任何修改即可处理这些格式：

```python
from comet_rag.pipeline import Pipeline

with Pipeline() as pipeline:
    result = pipeline.run("README.md")  # 使用前一段注册的同步 Markdown Hook
```

异步流水线接入同步文件提取函数时，必须显式使用 `sync_extractor()`。
它将同步提取移到线程，取消时先等线程结束，再允许释放正在读取的临时文件。
纯计算分块函数继续共用同步实现，无需增加 Async 副本：

```python
from comet_rag.engines.pipelines import AsyncPipelineHooks, PipelineConfig
from comet_rag.loaders import LoadedResource
from comet_rag.pipeline import AsyncPipeline
from comet_rag.ports import ExtractedDocument

hooks = AsyncPipelineHooks.fork()


@hooks.sync_extractor("txt", "log")
def extract_text(content: LoadedResource, config: PipelineConfig) -> ExtractedDocument:
    return ExtractedDocument(markdown=content.path.read_text(encoding="utf-8"))


async def read_text():
    async with AsyncPipeline(hooks=hooks) as pipeline:
        return await pipeline.run("notes.txt")
```

如果提取器已有原生异步实现，使用 `@hooks.extractor(...)` 注册 `async def`；
同步注册表的 `.extractor(...)` 则只注册同步函数。默认异步表也支持
`@AsyncPipelineHooks.sync_extractor(...)`，但局部 `fork()` 更容易明确修改范围。

---

## 7. 覆盖内置 Hook

对已支持的格式，也可以通过重新注册 hook 覆盖默认行为：

```python
from comet_rag.engines.documents.docx import DocxDocumentExtractor
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks
from comet_rag.loaders import LoadedResource
from comet_rag.ports import ExtractedDocument


# 自定义 DOCX extractor：保留页眉页脚，不保留图片
@PipelineHooks.extractor("docx")
def extract_docx_with_headers(
    loader_content: LoadedResource, config: PipelineConfig
) -> ExtractedDocument:
    extractor = DocxDocumentExtractor(
        include_headers_footers=True,
        include_images=False,
    )
    return extractor.extract(
        loader_content.path,
        filename=loader_content.metadata.get("file_name", loader_content.path.name),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
```

---

## 8. 底层模块独立使用

可以跳过 Pipeline，直接使用各子模块。

### Loader

```python
from comet_rag.loaders import AutoLoader, SourceContent

with AutoLoader.default() as loader:
    content = loader.load(SourceContent("document.docx"))
    try:
        print(content.path, content.metadata)
    finally:
        content.cleanup()  # 及时释放单个下载结果；本地原始文件不会被删除

    # 并发上限是显式参数；生产环境应按资源预算调整
    items = loader.batch_load(["a.txt", "b.txt"], max_concurrency=4)
    for item in items:
        item.cleanup()
```

同步与异步 Loader 分别使用 `AutoLoader` / `AsyncAutoLoader`，方法同名为
`load`、`batch_load`、`cleanup`；异步 Loader 的这些方法都要等待。
`LoadedResource` 是共用的值对象，单个结果的 `content.cleanup()` 仍为同步本地释放。
路由器按叶子 Loader 对批量输入分组，使 URL 连接池可复用，并保留各来源的并发策略。

`AutoLoader` 只暴露所有 Loader 共有的 `source` 和 `max_concurrency` 参数。
需要 `download_config`、自定义 HTTP client 等 URL 专用选项时，应直接使用
`URLLoader`，避免把某个 Loader 的参数误传给混合批次中的其他 Loader：

```python
from comet_rag.loaders import DownloadRequestConfig, URLLoader

loader = URLLoader()
config = DownloadRequestConfig(timeout=30, follow_redirects=False)
try:
    content = loader.load("https://example.com/report.docx", download_config=config)
finally:
    loader.cleanup()
```

这段是可执行示例，不是说明文字 —— `tests/unit/test_docs_examples.py` 会拿它
去比对真实签名。这条用法曾经在一次模板方法重构里悄悄失效（`load()` 只剩
`source` 一个参数），而当时它只是一句散文，守卫看不见。

MinIO/S3 适配器仍在基础设施层，但用户无需寻找第二个内部目录。只有实际创建客户端
时才需要安装 `server`（或 `all`）extra：

```python
import asyncio
from comet_rag.loaders import AsyncAutoLoader, AsyncLoaderRoute, AsyncS3Loader


async def load_from_object_storage():
    routes = AsyncAutoLoader.default_routes()
    routes.insert(
        0,
        AsyncLoaderRoute.schemes(
            "object-storage",
            AsyncS3Loader(
                endpoint_url="http://127.0.0.1:9010",
                access_key_id="minioadmin",
                secret_access_key="minioadmin",
            ),
            {"s3", "minio"},
        ),
    )
    async with AsyncAutoLoader(routes) as loader:
        content = await loader.load("s3://documents/report.pdf")
        try:
            return await asyncio.to_thread(content.path.read_bytes)
        finally:
            content.cleanup()
```

`LoaderContent` 是 `LoadedResource` 的兼容别名，旧导入路径继续可用；新代码应优先
使用 `comet_rag.loaders`，让公开入口与内部物理分层解耦。

### Converter + Parser + Cleaner

```python
from comet_rag.engines.documents.docx import DocxCleaner, DocxConverter, DocxParser

# content 来自 Loader；使用期间须保持 LoadedResource 有效
doc = DocxConverter(content).to_docx()
parsed = DocxParser().parse(doc)

# 获取结构化 blocks（保留语义层级）
blocks = DocxCleaner().clean_to_blocks(parsed)
for block in blocks:
    print(block["type"], block.get("content", "")[:60])

# 获取 markdown 字符串（供 Chunker 使用）
text = DocxCleaner(
    include_headers_footers=False,
    include_images=True,
).clean_to_markdown(parsed)
```

`DocxCleaner` 配置项：

| 参数 | 默认 | 说明 |
|------|------|------|
| `include_headers_footers` | `False` | 是否保留页眉/页脚 |
| `include_images` | `True` | 是否保留图片（以 `[image: alt]` 占位） |

### Chunker

```python
from comet_rag.engines.chunkers import (
    FixedSizeChunker,
    RecursiveChunker,
    code_profile,
)

# 无自然边界的确定性切分
fixed = FixedSizeChunker(chunk_size=1000, chunk_overlap=100)

# 段落 → 换行 → 空格 → 字符的递归切分
recursive = RecursiveChunker(chunk_size=1000, chunk_overlap=100)

# Rust 只是一组参数画像，返回的仍然是 RecursiveChunker
rust = RecursiveChunker.from_profile(
    code_profile("rs", chunk_size=1200, chunk_overlap=120)
)

drafts = rust.split(text)
print(drafts[0].start_char, drafts[0].end_char)

# 只需要文本时返回 list[str]
chunks = rust.chunk(text)
```

新策略汇总：

| 类名 | 用途 | 默认 size/overlap |
|------|------|------------------|
| `FixedSizeChunker` | 按长度预算硬切，也是超长单元的最终兜底 | 1000 / 0 |
| `RecursiveChunker` | 使用可配置 separator profile 递归切分 | 1000 / 0 |

`code_language` 支持 `py`、`ts`、`js`、`java`、`c`、`cpp`、`go`、`php`、`r`、
`rust` 和 `html`；也接受 `python`、`typescript`、`javascript`、`c++`、`.py`
和 `.rs` 等别名。`code_profile()` 只返回参数数据，不创建新的 Chunker 类型。
各 profile 的默认 `size/overlap` 为：

| `code_language` | py | ts | js | java | c | cpp | go | php | r | rust | html |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 默认值 | 1500/150 | 1500/150 | 1200/100 | 2000/200 | 1000/100 | 1500/150 | 1000/100 | 1200/100 | 1000/100 | 1500/150 | 1500/200 |

`chunk_size` 和 `chunk_overlap` 使用同一个 `length_function`；默认为
`len`，单位是 Unicode code point。需要 token 或字节预算时由调用方注入计数函数，
engines 不强绑 tokenizer 依赖。`chunk_overlap` 是上限目标：递归策略优先
保留完整语义单元，因此实际 overlap 可以更小，可通过相邻块的
`start_char/end_char` 直接观察。

文本、DOCX、Markdown、CSV、JSON、XML 和代码语言差异都由 `ChunkProfile` 表达；
运行时只有 `FixedSizeChunker`、`RecursiveChunker` 及未来真正具有不同算法的 Chunker。

文档级结构策略不会增加新的原子算法：

```python
from comet_rag.engines.chunkers import (
    MarkdownSectionStrategy,
    PageChunkingStrategy,
    RecursiveChunker,
)
from comet_rag.engines.documents import MarkdownDocumentNormalizer
from comet_rag.ports import ExtractedDocument

document = MarkdownDocumentNormalizer().normalize(
    ExtractedDocument(markdown="# 安装\n\n正文\n\n## Docker\n\n容器说明")
)
strategy = MarkdownSectionStrategy(
    RecursiveChunker(chunk_size=1000, chunk_overlap=100)
)
drafts = strategy.split(document)
print(drafts[0].metadata["heading_path"])
```

`MarkdownSectionStrategy` 不跨标题 section，`PageChunkingStrategy` 不跨真实页面。后者
要求 `NormalizedDocument.blocks` 已包含带 `page_number` 的 page blocks；缺失时明确
报错，不把换行或 Markdown 内容猜成分页。

---

## 9. 目前支持的文件格式

| 格式 | 扩展名 | Extractor | Chunker |
|------|--------|-----------|---------|
| Word 文档 | `.docx` `.doc` | ✅ 内置 | ✅ Markdown section 策略 |
| 纯文本 | `.txt` | 需自定义注册 | 单 section 策略 |
| Markdown | `.md` | 需自定义注册 | ✅ 默认分析标题 section |
| PDF | `.pdf` | ✅ 外部 MinerU；服务自动装配，纯库显式注册 | content list 可无损验证时按页，否则按 section |
| CSV | `.csv` | 待实现 | — |
| 代码文件 | `.py` `.ts` 等 | 待实现 | Chunker 已就绪 |

> 所有自定义注册见 [第 6 节](#6-自定义-hook-扩展新格式)。

PDF 会同时请求 MinerU 的 `md_content` 与 legacy `content_list`。只有 content list 能按页
无损重建出完全相同的 Markdown 时才启用 page 策略；复杂视觉块或协议不一致会保守回退
section 策略，绝不通过搜索重复文本来猜页边界。

---

## 10. PDF 与外部 MinerU

`engines` 不会在 import 时偷偷连接外部服务。参考服务在
`composition/bootstrap.py` 中根据配置创建 `AsyncMinerUDocumentExtractor`，向该
Context 的异步注册表注册 PDF Hook；因此启用 `infrastructure_config.mineru.enabled` 后，
`POST /ingest` 的 Local、URL 和 S3 PDF 都走同一条链路。

库调用方也可以显式完成同样的装配。异步入口不会阻塞事件循环，更适合外部解析：

```python
import asyncio

from comet_rag.engines.pipelines import AsyncPipelineHooks, PipelineConfig
from comet_rag.infrastructure.extractors import AsyncMinerUDocumentExtractor
from comet_rag.loaders import LoadedResource
from comet_rag.pipeline import AsyncPipeline
from comet_rag.ports import ExtractedDocument


async def parse_pdf():
    extractor = AsyncMinerUDocumentExtractor(
        "http://127.0.0.1:8989",
        backend="vlm-http-client",
    )
    hooks = AsyncPipelineHooks.fork()

    @hooks.extractor("pdf")
    async def extract_pdf(content: LoadedResource, config: PipelineConfig) -> ExtractedDocument:
        return await extractor.extract(
            content.path, filename=content.path.name, media_type="application/pdf",
        )

    try:
        async with AsyncPipeline(hooks=hooks) as pipeline:
            return await pipeline.run("document.pdf")
    finally:
        await extractor.close()


result = asyncio.run(parse_pdf())
print(result.chunks[0].text)
```

`base_url` 必须指向提供 protocol v2 `/health` 与 `/tasks` 的 `mineru-api` 或
`mineru-router`，不能直接指向只有 `/v1/models` 的 vLLM。上例没有绑定进程级
并发闸门，只适合单次库调用；服务和批量任务应使用组合根装配，以获得独立 MinerU
闸门、来源准入、重试、Task context 二次限长和统一资源关闭。

MinerU 部署、配置、错误语义与真实集成测试见
[MinerU 集成](mineru_integration.md)。

## 10. 有区间分块的覆盖契约

结构策略与 HierarchyBuilder 除逐项检查正文/span外，还拒绝遗漏非空原文、倒序区间，
包括父块或子块只返回头尾的情况；允许合法 overlap 和纯空白间隔。服务文档 Hook 的
全部结果有span时也按同一规则校验，在 embedding 与向量写入前失败。

旧字符串 Hook 或混合有/无span结果无法证明完整覆盖，保留兼容但不补造位置。新
文档 Hook 应返回可靠区间。覆盖不表示一个块包含整张长表，单元完整性须另外核验。
