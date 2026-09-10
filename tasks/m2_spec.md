# Spec: M2 PDF / MinerU

> 状态：已完成（v1.0）
> GitHub Issue：[#50](https://github.com/comet-7x/comet-rag/issues/50)
> 开发分支：`feature/m2-pdf-mineru`
> 最后更新：2026-09-10

## 1. 目标

让本地、URL 与 S3 来源的 PDF 复用 M1 入库链路：加载 → MinerU 提取 Markdown
→ 分块 → 向量化 → 写入知识库 → 检索命中。DOCX 行为与核心安装体积不得回退。

## 2. 非目标

- 不把 MinerU SDK、Torch、模型权重或 GPU 运行时加入任何依赖组。
- 不在 Comet-RAG 进程内导入、启动或管理 MinerU 服务。
- 不在本里程碑支持图片单独入库、图片检索或解析产物持久化。
- 不借机重构 DOCX 解析器、TaskStore、TaskExecutor 或向量库 schema。

## 3. 已确定设计

### D1 — 只接 HTTP 服务，不嵌入官方 Python SDK

MinerU 3.x 已提供 `mineru-api` 与接口兼容的 `mineru-router`。Comet-RAG 通过
`httpx` 适配它们，适配器放在 `infrastructure/providers/document/`；`engines/`
只依赖 Port 和规范化结果。这样默认安装、CPU worker 与 API 进程都不会带入
MinerU 的重依赖，本地或多 GPU 部署也能在 Comet-RAG 之外独立扩容。

项目不再提供 `mineru` extra。将来若要支持进程内 SDK，必须作为新规格重新评审，
不能在 HTTP 适配器中增加隐式 fallback。

### D2 — 使用异步任务接口

正常路径使用：

1. `GET /health` 校验健康状态与 `protocol_version`；
2. `POST /tasks` 上传一个 PDF 并取得 MinerU `task_id`；
3. `GET /tasks/{task_id}` 轮询终态；
4. `GET /tasks/{task_id}/result` 读取 JSON 中唯一结果的 `md_content`。

提交时显式固定 `response_format_zip=false`、`return_md=true`，其余产物开关全部
关闭；不能依赖 MinerU 的默认值。单次请求只上传一个 PDF，结果必须恰好包含一项，
且该项必须提供字符串 `md_content`，否则按协议错误失败。

`POST /file_parse` 只用于兼容测试，不作为生产默认。MinerU 的任务状态只在单个
服务进程内保存，重启或超过保留期后可能返回 404；适配器遇到 404 时可在同一总
超时预算内重提一次，不能无限重提，也不能把远端 task_id 当作永久记录。

### D3 — 新增通用文档提取 Port

在 `ports/document.py` 定义最小契约，词汇表使用 `dataclass(slots=True)`：

```python
@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    markdown: str
    metadata: dict[str, object]


class DocumentExtractorPort(Protocol):
    def extract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument: ...
    async def aextract(
        self, path: Path, /, *, filename: str, media_type: str
    ) -> ExtractedDocument: ...
    async def aclose(self) -> None: ...
```

同步与异步入口必须语义一致；注入的 HTTP client 归调用方，内部创建的 client
由适配器关闭。Port 只依赖标准库和值对象，不引用 `engines.LoaderContent`，也不
出现 MinerU 的 backend、server_url 或响应字段。

### D4 — 为 PipelineHooks 增加异步提取钩子

保留现有同步 `extractor()`，新增 `aextractor()` 与 `get_aextractor()`：

- `Pipeline.run()` 继续调用同步钩子；
- `Pipeline.arun()` 与 `IngestRunner` 优先调用异步钩子；
- 未注册异步钩子时，用 `asyncio.to_thread()` 执行同步钩子；
- 组合根把 MinerU 适配器的同步/异步方法注册为 PDF 钩子，`engines` 不 import
  `infrastructure`。

这是加法式扩展，不改变已有 DOCX hook 签名。

### D5 — M2 只消费 Markdown

请求固定 `response_format_zip=false`、`return_md=true`，关闭原始模型输出、中间
JSON、图片和原文件回传。
表格与公式保留在 Markdown 中。`content_list`、提取图片及其对象存储生命周期
留给后续里程碑，避免 M2 同时发明新的块模型和资产仓储。

## 4. 资源与安全边界

- MinerU 地址、backend 与解析选项只能来自服务配置，不能由 `/ingest` 请求覆盖，
  防止把解析器变成 SSRF 代理。
- 上传前沿用 Loader 的对象大小限制，并再次确认文件类型为 PDF；不一次性
  `read_bytes()`，使用文件句柄流式上传。
- 增加 MinerU 独立的进程级并发闸门，不与 embedding/rerank 或 loader 共用预算。
- 配置必须区分连接超时、单次 HTTP 超时、总解析超时与轮询间隔；所有默认值需在
  `config/schemas.py` 写明保护的资源，并由测试覆盖。
- 分别限制 MinerU HTTP 响应体与最终 Markdown 大小；在写入 Task context 前再次
  校验，不能让远端输出撑爆任务表或跨 worker 移交。
- 取消 Comet-RAG 任务时停止轮询并释放本地响应流。MinerU 当前没有稳定的取消
  契约，因此只停止本地等待，不承诺终止远端计算。
- 429、5xx、网络错误和远端任务丢失可重试；格式错误、超过资源限制和确定性
  4xx 直接失败。

首版保留现有 `extracting` CPU lane：异步 HTTP 不阻塞事件循环，但会占一个
worker job 名额。按格式动态分道需要修改任务路由语义，先用基准确认它确实成为
瓶颈，再单独设计。

## 5. 配置草案

```yaml
infrastructure_config:
  mineru:
    enabled: false
    base_url: http://127.0.0.1:8000
    backend: pipeline
    parse_method: auto
    language: ch
    formula: true
    table: true
    connect_timeout_seconds: 10.0
    request_timeout_seconds: 60.0
    upload_timeout_seconds: 300.0
    poll_interval_seconds: 1.0
    parse_timeout_seconds: 900
    max_response_bytes: 16777216
    max_markdown_bytes: 8388608

limits:
  mineru_concurrency: 2
  mineru_queue: 16
  mineru_wait_timeout: 30.0
```

这些默认值已由 T8 的小型文本 PDF 与扫描件验证可用。小样本不足以代表长文档
尾延迟，因此 M2 不据此收紧上限；生产部署仍应按自己的文档集与上游容量采样调整。

## 6. 测试与验收

### S1 — 分层与安装

- [x] `tests/unit/test_layering.py` 证明 `engines/` 不 import MinerU 或具体适配器。
- [x] `uv sync --no-default-groups` 后 DOCX/Loader/Pipeline 引擎测试仍通过。
- [x] 默认与 `all` 安装不下载 Torch、模型权重或 MinerU 本体。
- [x] `pyproject.toml` 与 `uv.lock` 不含 `mineru` 包或 `mineru` extra。

### S2 — 适配器契约

- [x] 用 `httpx.MockTransport` 覆盖 health、提交、排队、成功、失败、429/5xx、
  超时、404 重提、超大响应与取消清理。
- [x] 请求显式发送全部产物开关；JSON 结果必须恰好包含一个 `md_content`。
- [x] 同步/异步入口对同一响应生成相同 Markdown 和稳定 metadata。
- [x] 多次调用复用 client；`aclose()` 只关闭内部创建的资源。
- [x] 并发峰值不超过 `mineru_concurrency`，等待队列有界。

### S3 — Pipeline 与服务链路

- [x] `Pipeline.run/arun` 能解析 PDF，DOCX 快照不变。
- [x] 本地、URL、S3 三种 PDF 来源走同一个提取 Port。
- [x] `/ingest` 的阶段记录、重试、取消和断点续跑仍只通过 TaskStore 观察。
- [x] PDF 入库后 `/search` 能命中正文、表格与公式文本。

### S4 — 集成测试与文档

- [x] `COMET_TEST_MINERU_URL` 未设置或服务不可达时集成测试 skip，不 fail。
- [x] 可用时对小型文本 PDF 与扫描 PDF 跑真实 `POST /tasks` 全链路。
- [x] 重写 `docs/mineru_integration.md`，删除固定内网地址与旧版私有 API 示例。
- [x] 更新配置示例、部署说明、架构/流水线用法和 M2 状态。
- [x] 默认 `uv run pytest` 仍小于 10 秒（1687 passed，8.71s）。

T8 于 2026-09-10 使用 MinerU 3.4.5 / protocol v2 和远端 MinerU 2.5 vLLM 验证：
文本与扫描样本端到端分别为 2.10s / 3.11s，Markdown 9 B / 14 B，Comet-RAG
测试进程 Python 堆峰值约 1.45 MB。样本过小，不据此收紧面向真实长文档的超时或
大小上限；并发 2、总超时 900s、响应 16 MiB、Markdown 8 MiB 暂保持不变。

T9 最终验收：Ruff 与格式检查通过，Pyright `0 errors`；隔离 core-only 环境的
engines 测试 `362 passed, 1 skipped, 1 xfailed`；默认单测 `1687 passed`；
integration `6 passed, 136 skipped`；e2e `29 passed`。真实 MinerU 用例的
`1 passed in 3.50s` 记录见 T8。

## 7. 实施顺序

1. Port、结果值对象和契约测试。
2. MinerU HTTP 适配器、资源限制、错误分类与 MockTransport 单测。
3. 异步 Pipeline hook 与 DOCX 回归测试。
4. 配置、组合根、独立闸门和生命周期。
5. IngestRunner、API 端到端与真实 MinerU 集成测试。
6. 文档、基准和 M2 验收记录。

每步单独提交，提交信息使用 Conventional Commits。

## 8. 官方依据

- [MinerU 快速使用文档](https://github.com/opendatalab/MinerU/blob/master/docs/zh/usage/quick_usage.md)
- [MinerU API 客户端实现](https://github.com/opendatalab/MinerU/blob/master/mineru/cli/api_client.py)
- [MinerU FastAPI 实现](https://github.com/opendatalab/MinerU/blob/master/mineru/cli/fast_api.py)
- [MinerU 可选模块说明](https://opendatalab.github.io/MinerU/quick_start/extension_modules/)
