# Embedding、Reranker 与视觉模型

V2 按执行模式选择类型：同步使用 `Foo`，异步使用 `AsyncFoo`；两者的方法同名。
例如 `OpenAIEmbeddingModel.embed_query()` 直接返回向量，
`AsyncOpenAIEmbeddingModel.embed_query()` 必须 `await`。旧的 `aembed_query`、
`arank`、`adescribe`、`aclose` 等入口已移除，不保留兼容别名。

每个实例只持有当前模式的客户端。默认由适配器创建并关闭；通过 `client=` 注入的
客户端由调用方关闭。同步 `close()` 当场完成，异步 `close()` 必须等待。
下面独立出现的 `await` 片段应放进 `async def`，或在支持顶层 await 的环境中执行。

## 文本 Embedding

```python
import asyncio

from comet_rag.engines.embedding.async_batch import embed_documents
from comet_rag.infrastructure.models import AsyncQwen3VLEmbeddingModel


async def main():
    model = AsyncQwen3VLEmbeddingModel(
        base_url="http://localhost:8000/v1",
        model_name="Qwen/Qwen3-VL-Embedding-8B",
        api_key="EMPTY",
    )
    try:
        query_vector = await model.embed_query("哪份文档讨论了并发控制？")
        document_vectors = await embed_documents(
            model, ["文档一", "文档二"], max_concurrency=8,
        )
        print(query_vector, document_vectors)
    finally:
        await model.close()


asyncio.run(main())
```

`embed_query()` 与 `embed_document()` 表达不同任务语义。Qwen 分别选择 query 和
retrieval 指令；其他适配器也可以选择不同编码器。同步调用选择同步类和同步调度模块：

```python
from comet_rag.engines.embedding.batch import embed_documents
from comet_rag.infrastructure.models import OpenAIEmbeddingModel

model = OpenAIEmbeddingModel(
    base_url="https://api.openai.com/v1",
    model_name="text-embedding-3-small",
    api_key="...",
)
try:
    query_vector = model.embed_query("并发控制")
    vectors = embed_documents(model, ["文档一", "文档二"])
finally:
    model.close()
```

### 批量容量与并发

两个模块都导出 `embed_documents`：`engines.embedding.batch` 为同步调度，
`engines.embedding.async_batch` 为异步调度。它们负责切分请求与限制并发；模型只声明
`batch_limit`，即一次请求最多容纳多少篇文档。

```python
from comet_rag.engines.embedding.async_batch import embed_documents
from comet_rag.infrastructure.models import AsyncOpenAIEmbeddingModel

model = AsyncOpenAIEmbeddingModel(
    base_url="https://api.openai.com/v1",
    model_name="text-embedding-3-small",
    api_key="...",
    batch_limit=512,  # 默认 2048；兼容服务的实际容量需单独确认
)
try:
    vectors = await embed_documents(model, ["文档一", "文档二"], max_concurrency=4)
finally:
    await model.close()
```

OpenAI 适配器使用原生批量请求；Qwen 多模态适配器的 `batch_limit` 为 1。
调度函数会据此分批，`max_concurrency` 控制本次调用最多同时发出多少个请求。
进程总并发由注入的共享 Gate 约束，两项限制叠加生效。

需要控制单次请求时调用 `model.embed_batch(docs)`；异步模型需 `await`。
非空输入恰好一次往返，调用方须确保 `len(docs) <= model.batch_limit`；空输入直接返回空列表。

### 注入客户端

```python
import httpx

from comet_rag.infrastructure.models import AsyncQwen3VLEmbeddingModel

async with httpx.AsyncClient(timeout=30) as client:
    model = AsyncQwen3VLEmbeddingModel(
        base_url="http://localhost:8000/v1",
        model_name="Qwen/Qwen3-VL-Embedding-8B",
        api_key="EMPTY",
        client=client,
    )
    vector = await model.embed_query("一只猫")
    await model.close()  # 不关闭借用的 client；外层 async with 负责关闭
```

Qwen 同步类接收 `httpx.Client`，异步类接收 `httpx.AsyncClient`；OpenAI 同步类接收
`openai.OpenAI`，异步类接收 `openai.AsyncOpenAI`。原来的 `sync_client=` 和
`async_client=` 参数已移除。不要用同步类承接异步客户端。

## 本地图片与混合内容

```python
from pathlib import Path

from comet_rag.infrastructure.models import AsyncQwen3VLEmbeddingModel
from comet_rag.ports import ImageContent, MediaResource, TextContent

model = AsyncQwen3VLEmbeddingModel(
    base_url="http://localhost:8000/v1",
    model_name="Qwen/Qwen3-VL-Embedding-8B",
    api_key="EMPTY",
)
try:
    image = MediaResource(path=Path("/data/cat.png"), mimetype="image/png")
    image_vector = await model.embed_image(image)
    mixed_vector = await model.embed_content([
        TextContent("一只坐在窗边的猫"), ImageContent(image),
    ])
finally:
    await model.close()
```

`MediaResource` 必须且只能提供一种来源：`path=Path(...)`、`url="https://..."`，
或 `data=b"..."`。内存字节需同时给出 `mimetype`。本地文件经过准入与大小检查后转为
Data URL；远程 URL 经过部署侧准入策略。异步适配器把阻塞的本地读取与 DNS 检查移出
事件循环，网络请求仍使用原生异步客户端。取消时会等待准备线程结束，再返回取消异常，
以免调用方提前释放线程仍在读取的媒体；这不能强制中断慢磁盘或 DNS。

Qwen 的 `tokenize()`、`detokenize()`、`get_output_dim()` 和 `get_max_model_len()`
同样会访问模型服务；使用异步模型时这四个方法都要 `await`。

## Reranker

```python
from comet_rag.infrastructure.models import AsyncQwen3VLReranker
from comet_rag.ports import RerankDocument

reranker = AsyncQwen3VLReranker(
    base_url="http://localhost:8001/v1",
    model_name="Qwen/Qwen3-VL-Reranker-8B",
    api_key="EMPTY",
)
try:
    ranked = await reranker.rank(
        "找猫",
        [
            RerankDocument(id="cat", content="一只猫"),
            RerankDocument(id="dog", content="一只狗"),
        ],
        top_k=2,
    )
    for item in ranked:
        print(item.index, item.score, item.document.id, item.document.content)
finally:
    await reranker.close()
```

候选也可以直接传字符串。结果按相关度排序，`index` 保留原输入位置，`document`
保留候选内容。图片查询或候选使用同一组 `TextContent`、`ImageContent`、`MediaResource`。
同步代码使用 `Qwen3VLReranker.rank()`。`score()` 是接收供应商 DTO 的低层接口，
业务代码优先调用 `rank()`；两种类均不再提供 `ascore/arank`。

## 视觉描述

`OpenAIVisionModel.describe()` 与 `AsyncOpenAIVisionModel.describe()` 接收已编码的
图片数据和 MIME 类型。它们分别满足 `VisionDescriptionPort` 与
`AsyncVisionDescriptionPort`；同步 DOCX 清理器使用前者，异步清理器使用后者。
共享的 DocxConfig 可以携带任一模式的视觉模型；具体清理器、提取器和服务装配会在
联网前检查 `describe` 的执行形态，错配直接抛出 TypeError。纯解析器、分块器、配置
和值对象仍共用一份同步计算实现，不创建无 I/O 的 Async 副本。

## 服务装配与关闭

参考服务由 `composition.bootstrap.build_context()` 创建异步模型并注入图片策略，
Embedding 与 Reranker 绑定同一进程级 Gate。模型只在组合根构造，业务层依赖
`AsyncEmbeddingPort` / `AsyncRerankerPort`。`await context.close()` 统一逆序关闭
上下文拥有的资源；库调用方自行关闭模型，Pipeline 不接管注入模型的所有权。
