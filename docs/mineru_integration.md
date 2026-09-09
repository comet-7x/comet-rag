# MinerU 集成

> M2 状态：设计与排期阶段。实现进度见 `tasks/m2_spec.md` 和 `tasks/m2_todo.md`。

Comet-RAG 只通过 HTTP 连接外部 `mineru-api` 或 `mineru-router`。项目不提供
MinerU SDK extra，也不会在 API、worker 或库进程中加载 Torch、模型权重或 GPU
运行时。

```text
Comet-RAG preprocessor
        │  HTTP: /health、/tasks、/tasks/{id}、/tasks/{id}/result
        ▼
mineru-api 或 mineru-router
        │
        ▼
MinerU 解析后端 / GPU 服务
```

这样部署有三个边界：

- Comet-RAG 负责来源准入、任务状态、重试、并发限制、入库与检索。
- MinerU 服务负责 PDF 解析、模型生命周期、GPU 调度和服务端产物清理。
- `/ingest` 调用方不能覆盖 MinerU 地址、backend 或解析选项；这些只来自服务配置。

## M2 使用的协议

生产路径使用 MinerU 的异步任务接口：

1. `GET /health` 校验健康状态和协议版本。
2. `POST /tasks` 流式上传一个 PDF。
3. `GET /tasks/{task_id}` 轮询 `pending`、`processing`、`completed` 或 `failed`。
4. `GET /tasks/{task_id}/result` 获取最终 Markdown。

提交请求显式固定以下产物开关，不依赖 MinerU 默认值：

```text
response_format_zip=false
return_md=true
return_middle_json=false
return_model_output=false
return_content_list=false
return_images=false
return_original_file=false
client_side_output_generation=false
```

结果使用 JSON 格式。M2 每次只提交一个 PDF，因此 `results` 必须恰好包含一项，
并且该项必须有字符串字段 `md_content`。ZIP、图片资产、content list 和中间模型
输出不属于 M2。

## 手工验证 MinerU 服务

下面的示例只验证 MinerU HTTP 服务，不代表尚未完成的 Comet-RAG 适配器 API：

```python
import asyncio
from pathlib import Path

import httpx


async def parse_pdf(base_url: str, pdf_path: Path) -> str:
    async with httpx.AsyncClient(base_url=base_url, timeout=300) as client:
        health = await client.get("/health")
        health.raise_for_status()

        with pdf_path.open("rb") as stream:
            submitted = await client.post(
                "/tasks",
                files={"files": (pdf_path.name, stream, "application/pdf")},
                data={
                    "backend": "pipeline",
                    "parse_method": "auto",
                    "return_md": "true",
                    "return_middle_json": "false",
                    "return_model_output": "false",
                    "return_content_list": "false",
                    "return_images": "false",
                    "response_format_zip": "false",
                    "return_original_file": "false",
                    "client_side_output_generation": "false",
                },
            )
        submitted.raise_for_status()
        task = submitted.json()

        while True:
            status = await client.get(task["status_url"])
            status.raise_for_status()
            state = status.json()["status"]
            if state == "completed":
                break
            if state == "failed":
                raise RuntimeError(f"MinerU task failed: {status.text}")
            await asyncio.sleep(1)

        response = await client.get(task["result_url"])
        response.raise_for_status()
        results = response.json()["results"]
        if len(results) != 1:
            raise RuntimeError("expected exactly one MinerU result")
        return next(iter(results.values()))["md_content"]
```

正式适配器还会增加总解析 deadline、有界轮询、404 单次重提、响应大小限制、
取消清理和进程级并发闸门，不能直接把这个手工示例复制到生产代码。

## 不支持的集成方式

以下方式已明确排除：

- 在 Comet-RAG 中 import `mineru` 或 `mineru_vl_utils`。
- 通过 Python SDK 直接调用 `MinerUClient`。
- 从 worker 启动 `mineru` CLI 子进程。
- 在 Comet-RAG 配置中管理 MinerU 模型权重或 GPU。

若未来确有进程内解析需求，需要新建规格重新评审依赖、进程隔离与资源治理，
不得作为 HTTP 适配器的隐式 fallback 加入。

## 官方依据

- [MinerU FastAPI 实现](https://github.com/opendatalab/MinerU/blob/master/mineru/cli/fast_api.py)
- [MinerU 请求参数](https://github.com/opendatalab/MinerU/blob/master/mineru/cli/api_request.py)
- [MinerU API 客户端](https://github.com/opendatalab/MinerU/blob/master/mineru/cli/api_client.py)
