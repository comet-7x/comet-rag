# Pipeline 使用笔记

本文档记录 `comet_rag.engines.pipelines` 模块的用法，涵盖基本使用、配置、流式输出、批量处理、自定义 Hook 扩展，以及底层模块的独立使用方式。

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

---

## 1. 快速开始

```python
from comet_rag.engines.pipelines import Pipeline

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
from comet_rag.engines.pipelines import Pipeline


async def main():
    pipeline = Pipeline()
    result = await pipeline.arun("path/to/document.docx")
    print(len(result.chunks))


asyncio.run(main())
```

---

## 2. PipelineConfig 配置项

```python
from comet_rag.engines.pipelines import Pipeline, PipelineConfig

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
from comet_rag.engines.pipelines import Pipeline, PipelineConfig
from comet_rag.infrastructure.models.embedding.qwen3_vl_embedding import (
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
    ]  # 来自 LoaderContent（source_type, file_name, file_size...）
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

---

## 6. 自定义 Hook 扩展新格式

Pipeline 内部通过 `PipelineHooks` 注册表分发处理逻辑。增加新格式只需注册两个 hook：

- **extractor**：`(LoaderContent, PipelineConfig) → str`，负责将文件转换为清洁文本
- **chunker**（可选）：`(str, PipelineConfig) → list[str]`，自定义分块策略；不注册则回退到 `TextChunker`

> 两个 hook 都接收完整的 `PipelineConfig`，而不是散装的 `chunk_size` / `chunk_overlap`。
> 这样新增格式专属配置（如 `config.docx`）时无需改动 hook 签名。

```python
from comet_rag.engines.loaders.types import LoaderContent
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks


# 注册纯文本 extractor
@PipelineHooks.extractor("txt", "log")
def extract_plaintext(loader_content: LoaderContent, config: PipelineConfig) -> str:
    return loader_content.path.read_text(encoding="utf-8")


# 注册 Markdown extractor（可复用 TextChunker）
@PipelineHooks.extractor("md", "mdx")
def extract_markdown(loader_content: LoaderContent, config: PipelineConfig) -> str:
    return loader_content.path.read_text(encoding="utf-8")


# 为 markdown 注册专用 chunker
@PipelineHooks.chunker("md", "mdx")
def chunk_markdown(text: str, config: PipelineConfig) -> list[str]:
    from comet_rag.engines.chunkers.text_chunker import MdxChunker

    return MdxChunker(config.chunk_size, config.chunk_overlap).chunk(text)
```

注册之后，`Pipeline` 无需任何修改即可处理这些格式：

```python
result = Pipeline().run("README.md")
```

---

## 7. 覆盖内置 Hook

对已支持的格式，也可以通过重新注册 hook 覆盖默认行为：

```python
from comet_rag.engines.cleaners.docx_cleaner import DocxCleaner
from comet_rag.engines.converters.text_converter import DocxConverter
from comet_rag.engines.loaders.types import LoaderContent
from comet_rag.engines.parsers.docx_parser.docx_parser import DocxParser
from comet_rag.engines.pipelines import PipelineConfig, PipelineHooks


# 自定义 DOCX extractor：保留页眉页脚，不保留图片
@PipelineHooks.extractor("docx")
def extract_docx_with_headers(
    loader_content: LoaderContent, config: PipelineConfig
) -> str:
    doc = DocxConverter(loader_content).to_docx()
    parsed = DocxParser().parse(doc)
    return DocxCleaner(
        include_headers_footers=True,
        include_images=False,
    ).clean_to_markdown(parsed)
```

---

## 8. 底层模块独立使用

可以跳过 Pipeline，直接使用各子模块。

### Loader

```python
from comet_rag.engines.loaders.auto_loader import AutoLoader
from comet_rag.engines.loaders.types import SourceContent

loader = AutoLoader()
lc = loader.load(SourceContent("document.docx"))
print(lc.path, lc.metadata)
lc.cleanup()  # URL 下载的临时文件需手动清理（或用 with 语句）

# with 语句自动 cleanup
with AutoLoader() as loader:
    lc = loader.load("document.docx")
    # 并发上限是显式参数；默认安全上限为 10，生产环境应按资源预算调整
    items = loader.batch_load(["a.txt", "b.txt"], max_concurrency=4)
```

异步上下文会调用统一的 `acleanup()` 契约。`AutoLoader` 会按实际 loader
对批量输入分组，因此 `URLLoader` 可以复用连接池，本地或自定义 loader 也能
保留自己的并发策略。

MinIO/S3 等可选对象存储不需要修改 `SourceType` 或让 engines 依赖 SDK；在
基础设施层实现 `BaseLoader` 后注册一条路由即可：

```python
from comet_rag.engines.loaders import AutoLoader
from your_project.loaders import MinioLoader

loader = AutoLoader()
loader.register_loader(
    "minio",
    MinioLoader(...),
    lambda source: source.parsed_url.scheme in {"s3", "minio"},
    prepend=True,
)
result = await loader.aload("s3://documents/report.pdf")
```

### Converter + Parser + Cleaner

```python
from comet_rag.engines.converters.text_converter import DocxConverter
from comet_rag.engines.parsers.docx_parser.docx_parser import DocxParser
from comet_rag.engines.cleaners.docx_cleaner import DocxCleaner

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
from comet_rag.engines.chunkers.text_chunker import DocxChunker, TextChunker, MdxChunker

chunks = DocxChunker(chunk_size=1500, chunk_overlap=150).chunk(text)
```

可用 Chunker 汇总：

| 类名 | 适用格式 | 默认 size/overlap |
|------|---------|------------------|
| `TextChunker` | TXT | 1500 / 150 |
| `DocxChunker` | DOCX | 2500 / 250 |
| `MdxChunker` | MD/MDX | 3000 / 300 |
| `PythonChunker` | .py | 1500 / 150 |
| `TypeScriptChunker` | .ts | 1500 / 150 |
| `CsvChunker` | .csv | 1200 / 100 |
| `JsonChunker` | .json | 2000 / 200 |
| `XmlChunker` | .xml | 2500 / 250 |

---

## 9. 目前支持的文件格式

| 格式 | 扩展名 | Extractor | Chunker |
|------|--------|-----------|---------|
| Word 文档 | `.docx` `.doc` | ✅ 内置 | ✅ `DocxChunker` |
| 纯文本 | `.txt` | 需自定义注册 | 回退 `TextChunker` |
| Markdown | `.md` | 需自定义注册 | 需自定义注册 |
| PDF | `.pdf` | 待实现 | — |
| CSV | `.csv` | 待实现 | — |
| 代码文件 | `.py` `.ts` 等 | 待实现 | Chunker 已就绪 |

> 所有自定义注册见 [第 6 节](#6-自定义-hook-扩展新格式)。
