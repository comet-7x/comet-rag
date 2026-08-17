# Embedding 与 Reranker 使用指南

模型模块的常用接口按使用意图命名。业务调用者不需要了解 Qwen/OpenAI 的请求
DTO，也不需要手工把重排分数与原文重新对齐。

## 文本 Embedding

```python
from comet_rag.infrastructure.models import Qwen3VLEmbeddingModel

model = Qwen3VLEmbeddingModel(
    base_url="http://localhost:8000/v1",
    model_name="Qwen/Qwen3-VL-Embedding-8B",
    api_key="EMPTY",
)

query_vector = await model.aembed_query("哪份文档讨论了并发控制？")
document_vectors = await model.aembed_documents(
    ["文档一", "文档二"],
    max_concurrency=8,
)

await model.aclose()
```

`aembed_query()` 与 `aembed_documents()` 不是别名。Qwen 会分别使用 query 和
retrieval 指令；其他模型也可以选择不同编码器。同步代码使用对应的
`embed_query()` 与 `embed_documents()`。

OpenAI 兼容文本模型使用相同接口：

```python
from comet_rag.infrastructure.models import OpenAIEmbeddingModel

model = OpenAIEmbeddingModel(
    base_url="https://api.openai.com/v1",
    model_name="text-embedding-3-small",
    api_key="...",
)
vectors = await model.aembed_documents(["文档一", "文档二"])
```

该适配器会使用服务端原生批量能力，一批文档只发送一个请求。

## 本地图片与混合内容

图片来源使用 `MediaResource` 明确表达，不再把本地路径伪装成 `image_url`：

```python
from pathlib import Path

from comet_rag.infrastructure.models import Qwen3VLEmbeddingModel
from comet_rag.models import ImageContent, MediaResource, TextContent

image = MediaResource(path=Path("/data/cat.png"), mimetype="image/png")

image_vector = await model.aembed_image(image)
mixed_vector = await model.aembed_content(
    [
        TextContent("一只坐在窗边的猫"),
        ImageContent(image),
    ]
)
```

`MediaResource` 必须且只能提供一种来源：

- `path=Path(...)`：本地文件；适配器校验后转换为 Data URL。
- `url="https://..."`：远程图片；经过部署侧 URL 准入策略。
- `data=b"...", mimetype="image/png"`：内存字节。

## Reranker

简单文本可以直接传字符串；返回值已经按相关度排序：

```python
from comet_rag.infrastructure.models import Qwen3VLReranker

reranker = Qwen3VLReranker(
    base_url="http://localhost:8001/v1",
    model_name="Qwen/Qwen3-VL-Reranker-8B",
    api_key="EMPTY",
)

ranked = await reranker.arank(
    "并发闸门如何工作？",
    ["候选文档一", "候选文档二"],
    top_k=2,
)

for item in ranked:
    print(item.index, item.score, item.document.content)
```

需要保留业务 ID 和元数据时使用结构化候选：

```python
from comet_rag.models import RerankDocument

ranked = await reranker.arank(
    "找猫",
    [
        RerankDocument(id="cat", content="一只猫"),
        RerankDocument(id="dog", content="一只狗"),
    ],
)

assert ranked[0].document.id == "cat"
```

图片查询或候选使用与 Embedding 相同的 `TextContent`、`ImageContent` 和
`MediaResource`。`score/ascore` 与供应商请求 DTO 暂时保留给旧代码和高级用法，
新业务代码优先使用 `rank/arank`。

## 服务端装配

上述示例适合把 comet-rag 当库直接使用。运行参考服务时，不要在业务模块中自行
构造模型；`core.bootstrap` 会创建适配器、注入图片准入策略，并让 Embedding 与
Reranker 共享进程级并发闸门。
