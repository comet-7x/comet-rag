# MinerU Integration Notes

## Architecture Overview

The MinerU stack in this project has three distinct layers:

```
RAG Pipeline
    ↓
mineru-api server  (localhost:8989)   ← document parsing orchestrator
    ↓
vLLM server  (steins-middleware.steins.net:9909)  ← GPU-backed VLM inference
```

- **Port 9909** is an OpenAI/vLLM-compatible inference server (exposes `/v1/chat/completions`). It is the VLM backend MinerU uses for image/chart/text recognition.
- **Port 8989** is `mineru-api`, the document-level FastAPI server (exposes `/file_parse`, `/tasks`, `/health`). It orchestrates PDF parsing and calls the vLLM server for VLM inference.

These are two separate services. Do not confuse them.

---

## Integration Options

### Option 1: `mineru-api` HTTP (recommended for microservice)

Start the server:

```bash
MINERU_API_OUTPUT_ROOT=/path/to/output \
  mineru-api --host 0.0.0.0 --port 8989 \
  -b hybrid-http-client \
  -u http://steins-middleware.steins.net:9909
```

Call it from Python:

```python
import httpx


async def parse_pdf_via_api(pdf_path: str) -> dict:
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    filename = pdf_path.split("/")[-1]

    async with httpx.AsyncClient(timeout=900) as client:
        response = await client.post(
            "http://localhost:8989/file_parse",
            files={"files": (filename, pdf_bytes, "application/pdf")},
            data={
                "backend": "hybrid-http-client",
                "server_url": "http://steins-middleware.steins.net:9909",
                "effort": "high",  # "high" enables image/chart analysis
                "return_md": "true",
                "return_content_list": "true",
                "return_images": "false",
            },
        )
        response.raise_for_status()
        body = response.json()

    stem = filename.removesuffix(".pdf")
    result = body["results"][stem]
    return {
        "markdown": result["md_content"],
        "content_list": result.get("content_list"),
    }
```

**Key API facts:**

- Endpoint: `POST /file_parse` (sync) or `POST /tasks` (async)
- Response key under `results` is the **filename stem** (no extension)
- Batch: pass multiple files as `files=[("files", ...), ("files", ...)]`
- `effort="medium"` is faster but disables image/chart analysis
- `effort="high"` enables image/chart analysis (slower)
- Output path is **not configurable per request** — controlled server-side via `MINERU_API_OUTPUT_ROOT` env var (default `./output`). Server auto-cleans files after 24h.

**Full form parameters:**

| Field                   | Default           | Description                                                     |
| ----------------------- | ----------------- | --------------------------------------------------------------- |
| `files`               | required          | PDF / image / DOCX / PPTX / XLSX                                |
| `backend`             | `hybrid-engine` | `pipeline`, `vlm-http-client`, `hybrid-http-client`, etc. |
| `server_url`          | `None`          | VLM server URL for`*-http-client` backends                    |
| `effort`              | `medium`        | `medium` or `high` (hybrid backends only)                   |
| `parse_method`        | `auto`          | `auto`, `txt`, `ocr` (pipeline/hybrid only)               |
| `lang_list`           | `["ch"]`        | OCR language hint                                               |
| `formula_enable`      | `True`          | Enable formula parsing                                          |
| `table_enable`        | `True`          | Enable table parsing                                            |
| `image_analysis`      | `True`          | Enable image/chart analysis                                     |
| `return_md`           | `True`          | Return markdown in response                                     |
| `return_content_list` | `False`         | Return structured content list                                  |
| `return_middle_json`  | `False`         | Return internal middle JSON                                     |
| `return_model_output` | `False`         | Return raw model output JSON                                    |
| `return_images`       | `False`         | Return extracted images (base64)                                |
| `response_format_zip` | `False`         | Return ZIP file instead of JSON                                 |
| `start_page_id`       | `0`             | First page to parse (0-indexed)                                 |
| `end_page_id`         | `99999`         | Last page to parse (0-indexed)                                  |

---

### Option 2: Python library directly (`MinerUClient`)

Use this when you need full control over output paths or want to embed parsing directly in your process without running an extra server.

```python
import asyncio
from pathlib import Path
import pypdfium2 as pdfium
from pdf2image import convert_from_path
from mineru_vl_utils import MinerUClient
from mineru.backend.vlm.model_output_to_middle_json import result_to_middle_json
from mineru.backend.vlm.vlm_middle_json_mkcontent import union_make
from mineru.utils.pdfium_guard import open_pdfium_document
from mineru.data.data_reader_writer.filebase import FileBasedDataWriter
from mineru.utils.pdf_image_tools import load_images_from_pdf_doc
from mineru.utils.enum_class import MakeMode


def get_mineru_client() -> MinerUClient:
    return MinerUClient(
        backend="http-client",
        server_url="http://steins-middleware.steins.net:9909",
        max_concurrency=10,
        http_timeout=3600,
        image_analysis=True,
    )


async def parse_pdf(pdf_path: str, output_dir: str) -> str:
    images_dir = Path(output_dir) / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    pdf_bytes = Path(pdf_path).read_bytes()
    pdf_doc = open_pdfium_document(pdfium.PdfDocument, pdf_bytes)
    image_list = load_images_from_pdf_doc(
        pdf_doc=pdf_doc,
        start_page_id=0,
        end_page_id=len(pdf_doc) - 1,
        pdf_bytes=pdf_bytes,
    )

    client = get_mineru_client()
    images_pil = convert_from_path(pdf_path, dpi=300, fmt="png")
    model_outputs = await client.aio_batch_two_step_extract(images_pil)

    images_writer = FileBasedDataWriter(parent_dir=str(images_dir))
    middle_json = result_to_middle_json(
        model_outputs, image_list, pdf_doc, images_writer
    )

    markdown = union_make(middle_json["pdf_info"], MakeMode.MM_MD, images_dir)
    return markdown
```

**Processing pipeline:**

```
PDF bytes
  → pdf2image  → PIL images (one per page)
  → MinerUClient.aio_batch_two_step_extract()
      Step 1: layout detect  → ContentBlock list (bbox + type per block)
      Step 2: content extract → text per block (via vLLM /v1/chat/completions)
  → result_to_middle_json()  → internal middle JSON
  → union_make(MakeMode.MM_MD)  → markdown string (in memory)
```

**Output control:**

- `FileBasedDataWriter(parent_dir=...)` controls where images are saved
- Markdown string is returned in memory — no file write needed
- Also available: `MakeMode.CONTENT_LIST`, `MakeMode.CONTENT_LIST_V2`

---

### Option 3: CLI subprocess (original approach)

Use when running as a one-off tool or when you need the CLI's file-writing behavior.

```python
MINERU_CMD = [
    "mineru",
    "-p",
    "{pdf}",
    "-o",
    "{outdir}",
    "-b",
    "hybrid-http-client",
    "-u",
    "http://steins-middleware.steins.net:9909",
    "--effort",
    "high",
]
```

Cold-start overhead per invocation is low because the heavy VLM work goes to the remote server. Output is written to `{outdir}/{stem}/` as files on disk.

---

## Comparison

|                     | CLI subprocess        | Python library                      | `mineru-api` HTTP                           |
| ------------------- | --------------------- | ----------------------------------- | --------------------------------------------- |
| Extra server        | No                    | No                                  | Yes (`mineru-api`)                          |
| Output path control | Full (via`-o`)      | Full (via`FileBasedDataWriter`)   | Server-side only (`MINERU_API_OUTPUT_ROOT`) |
| Concurrency         | Semaphore + processes | `asyncio` + semaphore             | Server queue (max 3 concurrent by default)    |
| Content in memory   | No (read from disk)   | Yes (`union_make` returns string) | Yes (JSON response)                           |
| Best for            | Simple scripts        | Embedded in RAG service             | Microservice / multi-client                   |

---

## `MinerUClient` Key Methods

| Method                                 | Description                                                |
| -------------------------------------- | ---------------------------------------------------------- |
| `two_step_extract(image)`            | Sync: layout detect + content extract for one page         |
| `aio_two_step_extract(image)`        | Async single page                                          |
| `batch_two_step_extract(images)`     | Sync batch (all pages)                                     |
| `aio_batch_two_step_extract(images)` | Async batch — use this for RAG                            |
| `layout_detect(image)`               | Layout only (returns`ExtractResult` with bounding boxes) |
| `content_extract(image, type)`       | Extract content from a single block type                   |

`ExtractResult` is a `list[ContentBlock]`. Each `ContentBlock` is a dict with:

- `type`: `"text"`, `"title"`, `"table"`, `"image"`, `"chart"`, `"equation"`, etc.
- `bbox`: `[x1, y1, x2, y2]` normalized to `[0, 1]`
- `content`: extracted text string
- `angle`: rotation angle (`None`, `0`, `90`, `180`, `270`)

