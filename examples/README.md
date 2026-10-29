# V2 实际调用示例

这些示例直接组合独立组件，不启动 HTTP API、worker 或任务服务。需要 Python >=3.12；所有输出使用新的目录与 `index_scope`。旧数据不会自动升级或切换。

## 依赖与模型

```bash
uv sync --locked --extra postgres --extra milvus --extra sources --extra images --extra local-models
```

开发依赖中的 python-docx 仅用来生成示例 DOCX。核心 DOCX parser 直接读取受限 OOXML，不依赖 python-docx。

示例使用已下载到本地的 safetensors，不下载模型或执行远端模型代码：

| 模型 | 固定 revision | 权重 SHA-256 |
|---|---|---|
| `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` | `1a25a446712ba5ee05982a381eed697ef9b435cf` | `74813fbcdc750f235c9784c367ca1394d2a5c25eb0aac92761752ac239db7cff` |
| `cross-encoder/ms-marco-MiniLM-L6-v2` | `233902d25c440f23af6f7d6e94d2946bac0bee0a` | `821d1aa69520101d6e0737f78a042ae25b19e5cb9160701909d10434f4aeb0ae` |

CLIP 的 512 维文本和单图片处于同一空间，预处理文件指纹进入 `space_id`。它明确拒绝联合图文和多图片输入；本地 cross encoder 只接受文字。英文小样本不证明中文或领域检索质量。

## 持久文本与原生 DOCX

由调用方提供专用 PostgreSQL SQLAlchemy URL 环境变量 `COMET_RAG_TEST_POSTGRES_URL`，并先启动独立后端。两个持久示例均可用 `--milvus-db` 选择已存在的 Milvus 数据库，默认 `default`；ingest 与 verify 必须使用相同数据库。不要在命令行或结果文件中保存凭据。

```bash
python -m examples.persistent_text \
  --model-dir /absolute/path/to/local-clip \
  --scope example-text-build-001 \
  --output-dir tmp/example-text-build-001 \
  --milvus-uri http://127.0.0.1:29530 \
  --phase ingest

python -m examples.persistent_text \
  --model-dir /absolute/path/to/local-clip \
  --scope example-text-build-001 \
  --output-dir tmp/example-text-build-001 \
  --milvus-uri http://127.0.0.1:29530 \
  --phase verify
```

`ingest` 生成三个文本和一个原生表格 DOCX，实际嵌入并写入 PostgreSQL/Milvus，核对幂等重试、精确 record 指纹、结构读回和 dense/keyword/hybrid。`verify` 用新的进程和客户端读回同一清单；keyword 不注入查询模型。两个命令之间重启自己的专用后端，可以独立检查服务重启后的读回。

## HTTP 图片、文档图片与四种组合

`--image-dir` 提供 `cat.png` 与 `dog.jpg`，分别是真实猫、狗图片。验证使用过的公开样本为 [COCO 猫图](https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/coco_sample.png)和 [PyTorch Hub 狗图](https://raw.githubusercontent.com/pytorch/hub/master/images/dog.jpg)：

| 文件 | SHA-256 |
|---|---|
| `cat.png` | `cf6f3c4befa148732c7453e0de5afab00f682427435fead2d88b07a9615cdac2` |
| `dog.jpg` | `f3f87bb8ab3c26c7ecfd3ac60421d7f32b0503d1d6c5baf8bac42ed93d86351a` |

```bash
python -m examples.persistent_multimodal \
  --model-dir /absolute/path/to/local-clip \
  --image-dir /absolute/path/to/image-fixtures \
  --scope example-images-build-001 \
  --output-dir tmp/example-images-build-001 \
  --milvus-uri http://127.0.0.1:29530 \
  --phase ingest

python -m examples.persistent_multimodal \
  --model-dir /absolute/path/to/local-clip \
  --scope example-images-build-001 \
  --output-dir tmp/example-images-build-001 \
  --milvus-uri http://127.0.0.1:29530 \
  --phase verify
```

入库过程在回环随机端口启动临时图片服务器，真实 HTTP 下载→图片解析→稳定 Asset→真实 CLIP→PostgreSQL/Milvus。下载上下文结束后临时文件释放，服务器和客户端关闭；调用方保留输出目录。原图不会删除。另生成包含真实猫图的 DOCX，通过原生解析路径保存并检索内嵌图。

每个阶段用猫/狗分别检查文本→文本、文本→图片、图片→文本、图片→图片，共八次检索，并保存原始 dense 分数。图片查询使用已保存的 Asset；不通过文件名或隐式 OCR 替代图片输入。同图查询与两个对象只证明闭环和简单区分，不代表泛化准确率。

## 独立组件

```python
from pathlib import Path

from comet_rag.chunkers import DocumentChunker, RecursiveTextChunker
from comet_rag.documents.parsers import AsyncDocxParser
from comet_rag.documents.projection import project_document
from comet_rag.sources import AsyncLocalSourceResolver, LocalSource

async def parse_and_chunk(path: Path):
    async with AsyncLocalSourceResolver().resolve(LocalSource(path)) as resource:
        parsed = await AsyncDocxParser().parse(resource, output_dir=Path("retained-assets"))
    projection = project_document(parsed.document)
    chunks = DocumentChunker(RecursiveTextChunker(1000)).chunk(parsed.document)
    return parsed.report, projection, chunks
```

结构块与 Asset 是主数据，投影为有来源的 Python 字符范围。不可切分的表格/媒体超过 chunk 预算会失败，调用方显式选择合适预算。`partial` 必须由调用方检查，入库 pipeline 默认拒绝；不要把无原生页码的 DOCX 宣称为分页解析。

示例创建的客户端按创建顺序逆序关闭，构造、准备、取消或关闭失败时仍排干其他已创建资源，原始阶段错误保留。持久目录跨进程应保持可读。资料或切分/表示/模型配置变化使用新 scope，验证后由调用方切换搜索实例。删除只按 manifest 明确 IDs 执行，Asset 不随删除自动清理。
