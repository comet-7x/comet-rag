# Pipeline 使用笔记

本文档记录公共入口 `comet_rag.pipeline` 及其配置的用法，涵盖基本使用、流式输出、批量处理、自定义 Hook 扩展，以及底层模块的独立使用方式。DOCX 是纯库内置能力；PDF 需要外部 MinerU，并由服务组合根或库调用方显式注册。

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
pipeline = Pipeline()
result = pipeline.run("path/to/document.docx")

print(f"共 {len(result.chunks)} 个 chunk")
for chunk in result.chunks:
    print(chunk.text[:100])
```

异步版本：

```python
import asyncio
from comet_rag.pipeline import Pipeline


async def main():
    pipeline = Pipeline()
    result = await pipeline.arun("path/to/document.docx")
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

pipeline = Pipeline(
    config=PipelineConfig(embed=True),
    embedding_model=embedding_model,
)

result = pipeline.run("document.docx")
print(result.chunks[0].embedding)  # list[float]
```

---

## 3. 流式输出

适合大文件场景，每个 chunk 处理完立即 yield，无需等待全部完成。

```python
# 同步流式
for chunk in pipeline.stream_run("document.docx"):
    print(f"chunk {chunk.metadata['chunk_index']}: {chunk.text[:80]}")
```

```python
# 异步流式
async def stream():
    async for chunk in pipeline.astream_run("document.docx"):
        print(chunk.text[:80])


asyncio.run(stream())
```

> **注意**：`astream_run` 返回的是 `AsyncGenerator`，需要用 `async for` 迭代，不能 `await`。

---

## 4. 批量处理

```python
sources = [
    "docs/report_2024.docx",
    "docs/manual.docx",
    "https://example.com/spec.docx",  # 支持 URL
]

# 同步批量（线程池并发）
results = pipeline.batch_run(sources)

# 异步批量（asyncio 并发）
results = await pipeline.abatch_run(sources)

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

---

## 6. 自定义 Hook 扩展新格式

Pipeline 内部通过 `PipelineHooks` 注册表分发处理逻辑。增加新格式只需注册两个 hook：

- **extractor**：`(LoadedResource, PipelineConfig) → ExtractedDocument`，负责把文件映射为通用提取结果
- **document_chunker**（可选）：`(NormalizedDocument, PipelineConfig) → list[ChunkDraft]`，自定义分块策略；不注册则回退到 `RecursiveChunker`

所有提取结果都会在进入 Chunker 前经过统一的 `MarkdownDocumentNormalizer`。

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


# 注册 Markdown extractor（可复用 TextChunker）
@PipelineHooks.extractor("md", "mdx")
def extract_markdown(
    loader_content: LoadedResource, config: PipelineConfig
) -> ExtractedDocument:
    return ExtractedDocument(
        markdown=loader_content.path.read_text(encoding="utf-8")
    )


# 为 markdown 注册文档级 chunker
@PipelineHooks.document_chunker("md", "mdx")
def chunk_markdown(
    document: NormalizedDocument, config: PipelineConfig
) -> list[ChunkDraft]:
    return RecursiveChunker(
        config.chunk_size,
        config.chunk_overlap,
    ).split(document)
```

旧 `PipelineHooks.chunker(str, config) -> list[str]` 仍可运行，但会发出
`DeprecationWarning`，且无法提供可信的字符位置；请在 0.3.0 前迁移到
`document_chunker`。

注册之后，`Pipeline` 无需任何修改即可处理这些格式：

```python
result = Pipeline().run("README.md")
```

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

loader = AutoLoader.default()
lc = loader.load(SourceContent("document.docx"))
print(lc.path, lc.metadata)
lc.cleanup()  # URL/S3 下载的临时文件在消费后立即释放

# with 语句自动 cleanup
with AutoLoader.default() as loader:
    lc = loader.load("document.docx")
    # 并发上限是显式参数；默认安全上限为 10，生产环境应按资源预算调整
    items = loader.batch_load(["a.txt", "b.txt"], max_concurrency=4)
```

异步上下文会调用统一的 `acleanup()` 契约。`AutoLoader` 会按实际 loader
对批量输入分组，因此 `URLLoader` 可以复用连接池，本地或自定义 loader 也能
保留自己的并发策略。

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
from comet_rag.loaders import AutoLoader, LoaderRoute, S3Loader


async def load_from_object_storage():
    routes = AutoLoader.default_routes()
    routes.insert(
        0,
        LoaderRoute.schemes(
            "object-storage",
            S3Loader(
                endpoint_url="http://127.0.0.1:9000",
                access_key_id="minioadmin",
                secret_access_key="minioadmin",
            ),
            {"s3", "minio"},
        ),
    )
    async with AutoLoader(routes) as loader:
        content = await loader.aload("s3://documents/report.pdf")
        try:
            return content.path.read_bytes()
        finally:
            content.cleanup()
```

`LoaderContent` 是 `LoadedResource` 的兼容别名，旧导入路径继续可用；新代码应优先
使用 `comet_rag.loaders`，让公开入口与内部物理分层解耦。

### Converter + Parser + Cleaner

```python
from comet_rag.engines.documents.docx import DocxCleaner, DocxConverter, DocxParser

# lc 来自 Loader
doc = DocxConverter(lc).to_docx()
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
    CodeRecursiveChunker,
    FixedSizeChunker,
    RecursiveChunker,
)
from comet_rag.ports import NormalizedDocument

document = NormalizedDocument(markdown=text)

# 无自然边界的确定性切分
fixed = FixedSizeChunker(chunk_size=1000, chunk_overlap=100)

# 段落 → 换行 → 空格 → 字符的递归切分
recursive = RecursiveChunker(chunk_size=1000, chunk_overlap=100)

# 所有代码语言共用一个算法，code_language 只选择语言画像
code = CodeRecursiveChunker(
    code_language="rs", chunk_size=1200, chunk_overlap=120
)

drafts = code.split(document)
print(drafts[0].start_char, drafts[0].end_char)

# 兼容旧用法：只需要文本时仍可返回 list[str]
chunks = code.chunk(text)
```

新策略汇总：

| 类名 | 用途 | 默认 size/overlap |
|------|------|------------------|
| `FixedSizeChunker` | 按长度预算硬切，也是超长单元的最终兜底 | 1000 / 0 |
| `RecursiveChunker` | 使用可配置 separator profile 递归切分 | 1000 / 0 |
| `CodeRecursiveChunker` | 代码递归切分，通过 `code_language` 选择画像 | 随语言画像 |

`code_language` 支持 `py`、`ts`、`js`、`java`、`c`、`cpp`、`go`、`php`、`r`、
`rust` 和 `html`；也接受 `python`、`typescript`、`javascript`、`c++`、`.py`
和 `.rs` 等别名。代码分块只有这一个公开类，不再按语言维护子类。
各 profile 的默认 `size/overlap` 为：

| `code_language` | py | ts | js | java | c | cpp | go | php | r | rust | html |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 默认值 | 1500/150 | 1500/150 | 1200/100 | 2000/200 | 1000/100 | 1500/150 | 1000/100 | 1200/100 | 1000/100 | 1500/150 | 1500/200 |

`chunk_size` 和 `chunk_overlap` 使用同一个 `length_function`；默认为
`len`，单位是 Unicode code point。需要 token 或字节预算时由调用方注入计数函数，
engines 不强绑 tokenizer 依赖。`chunk_overlap` 是上限目标：递归策略优先
保留完整语义单元，因此实际 overlap 可以更小，可通过相邻块的
`start_char/end_char` 直接观察。

原有 `TextChunker`、`DocxChunker`、`MdxChunker`、`CsvChunker`、`JsonChunker` 和
`XmlChunker` 在整个 0.2.x 继续作为兼容入口，最早在 0.3.0 移除。

---

## 9. 目前支持的文件格式

| 格式 | 扩展名 | Extractor | Chunker |
|------|--------|-----------|---------|
| Word 文档 | `.docx` `.doc` | ✅ 内置 | ✅ `DocxChunker` |
| 纯文本 | `.txt` | 需自定义注册 | 回退 `TextChunker` |
| Markdown | `.md` | 需自定义注册 | 需自定义注册 |
| PDF | `.pdf` | ✅ 外部 MinerU；服务自动装配，纯库显式注册 | 回退 `TextChunker` |
| CSV | `.csv` | 待实现 | — |
| 代码文件 | `.py` `.ts` 等 | 待实现 | Chunker 已就绪 |

> 所有自定义注册见 [第 6 节](#6-自定义-hook-扩展新格式)。

---

## 10. PDF 与外部 MinerU

`engines` 不会在 import 时偷偷连接外部服务。参考服务在
`composition/bootstrap.py` 中根据配置创建 `MinerUDocumentExtractor`，同时注册
同步和异步 PDF Hook；因此启用 `infrastructure_config.mineru.enabled` 后，
`POST /ingest` 的 Local、URL 和 S3 PDF 都走同一条链路。

库调用方也可以显式完成同样的装配。异步入口不会阻塞事件循环，更适合外部解析：

```python
import asyncio

from comet_rag.composition.bootstrap import wire_pdf_extractor
from comet_rag.engines.pipelines import PipelineHooks
from comet_rag.infrastructure.extractors import MinerUDocumentExtractor
from comet_rag.pipeline import Pipeline


async def parse_pdf():
    extractor = MinerUDocumentExtractor(
        "http://127.0.0.1:8989",
        backend="vlm-http-client",
    )
    try:
        with PipelineHooks.temporary():
            wire_pdf_extractor(extractor)
            return await Pipeline().arun("document.pdf")
    finally:
        await extractor.aclose()


result = asyncio.run(parse_pdf())
print(result.chunks[0].text)
```

`base_url` 必须指向提供 protocol v2 `/health` 与 `/tasks` 的 `mineru-api` 或
`mineru-router`，不能直接指向只有 `/v1/models` 的 vLLM。上例没有绑定进程级
并发闸门，只适合单次库调用；服务和批量任务应使用组合根装配，以获得独立 MinerU
闸门、来源准入、重试、Task context 二次限长和统一资源关闭。

MinerU 部署、配置、错误语义与真实集成测试见
[MinerU 集成](mineru_integration.md)。
