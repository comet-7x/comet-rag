# 阶段 C 来源与文档保真

2026-10-29：本地、HTTP、原生 DOCX、公开 AWS S3 匿名只读与 MinerU 4.0.12 Flash 原生文字 PDF 的真实验收通过，阶段 C 在这些首版边界内完成。MinerU 仅保留 Markdown 并报告 partial；扫描 OCR、Basic/Standard/Advanced、兼容 S3、认证与版本对象行为未验证。

## 来源

`LocalSource` 借用原件，读取实际字节 SHA-256。`HttpSource` 与 `S3Source` 通过 async context manager 提供完整本地资源；由该上下文创建的临时目录在成功、异常和取消时释放。阻塞 SDK 取消后等线程结束才清理输入，注入客户端由创建者关闭。

HTTP 使用注入的 httpx client 和必选准入策略，每次重定向发送前校验；不跨域重新注入认证。限制重定向次数、声明和实际字节数，拒绝部分响应和非 identity 内容编码。保留 ETag/Last-Modified 等版本事实，它们不冒充实际内容 hash。带已知敏感 query 的 URL 要求显式 source_id，不持久保存临时签名定位。准入回调不保证 DNS 与实际连接 IP 绑定，这由调用方的网络策略负责。

S3 使用显式 namespace 与注入的同步 boto3-compatible client；保留 Bucket/Key/VersionId，请求的 VersionId 不得被替换。流式读取与实际预算/hash 核验，Body 必须关闭；ETag、VersionId、LastModified 和 SHA 分开保存。

33 项 remote 单元测试和两项真实回环 HTTP 集成验证逐跳准入、认证剥离、字节预算/长度、来源、临时目录与取消所有权。单元测试不访问真实服务。

## 原生 DOCX

直接映射 OOXML：段落/标题/列表、run 样式继承、字体、链接、字段、内容控件、tabs/newlines、表格/合并/网格缺口/嵌套、图片、页眉页脚。OMML 保存原生表达式及 partial 诊断，不声称全部公式已转为 LaTeX。

ZIP/XML、元素与解压预算、非法引用和不支持的 meaningful 内容都有反例测试。真实生成的 DOCX 及图片进入持久检索示例；未构造虚假页码或依赖旧 JSON 快照。`complete` 仅表示无已知遗漏，不保证 Word 所有格式已支持。

## Markdown 导出

`documents.exporters.export_markdown(document)` 同步从当前 blocks 和 Asset 引用派生 Markdown，不保留旧缓存。36 项单元测试实际解析生成的 TXT/DOCX，覆盖空白、富文本、链接、合并/嵌套表格、代码围栏和图片引用；OMML/MathML 与歧义公式保留源码及诊断。核心 wheel 的双进程读回也核对相同快照的导出结果。未对外部 Markdown 渲染器做浏览器验收。

## MinerU

显式导入 `documents.parsers.mineru.AsyncMinerUParser`，使用 MinerU 4.x 自托管服务的 `/v1/file_parse` 单请求接口。核对版本为 `4.0.12`，官方 tag `mineru-4.0.12-released` 指向源码 commit `b7f3dd0c5f6e6d66a298239ffd02b328b873918e`。调用方声明实际部署的 4.x release，适配器核对 `files[].parse.parser_version`，库不强绑一个补丁版本。

选型按五步核对：外部 PDF 解析确有需要；复用现有注入式 `httpx.AsyncClient` 和输入校验；标准库完成 JSON/版本校验；官方 `MinerUApiParser.parse_async` 已提供异步 SDK，但完整 MinerU 包会引入服务、文档库与模型依赖，没有独立轻量客户端发行包；官方单请求 HTTP 接口已涵盖本库需求，因此直接上传原始字节，不另写上传/任务轮询框架，不增加 MinerU 运行时依赖。

参数使用 query 中的 `tier`、`ocr_mode`、`output_formats=markdown`、`response_format=json`、`wait_timeout`、受控文件名和实际 SHA。上传前实际读字节并核对 hash/size/预算、PDF 头/尾；请求声明 Content-Length。响应校验任务/文件状态、tier、格式、实际 parser version、JSON 类型/重复键、编码/长度与字节预算，不跟随重定向。202 抛出包含 task_id 的 TimeoutError；上游任务可能继续，适配器不自动重传或冒称取消。

只请求 Markdown，原样保存一个 text Block，始终 `partial`、`coverage=None`；不伪造页、原生表格或图片 Asset。tier/ocr_mode 是请求配置，不能据此声称已验收 OCR 或 VLM 质量。官方 [HTTP 文档](https://github.com/opendatalab/MinerU/blob/b7f3dd0c5f6e6d66a298239ffd02b328b873918e/docs/en/usage/http_api.md)明确便捷路由属于自托管服务，不能用官方云 API 的 health 成功代替此路由的真实验收。单元测试只证明协议映射和失败边界。

## 公开 AWS S3 真实验收

`tests/integration/test_public_s3.py` 实际访问 [AWS Open Data 收录的 NOAA GHCN](https://registry.opendata.aws/noaa-ghcn/) 公开对象 `s3://noaa-ghcn-pds/ghcnd-version.txt`。使用 boto3 1.43.120、us-east-1 的 S3 endpoint 与 `UNSIGNED`；禁用本机 AWS 配置、凭据提供链和代理读取，不需要账户，不写入或删除远端对象。结果为 **1 passed，14.75 秒**。

独立 HTTPS 下载提供预期字节并与 SDK 的 ETag 对照，实际 resolver 下载后核对 bytes、SHA-256、ETag、LastModified、大小、locator 与稳定 source_id。真正解析 UTF-8 文本后释放下载目录，快照仍可读；再次获取身份相同，缩小预算会拒绝交付并清理，注入 client 在创建者关闭前仍能调用。

本轮对象为 270 字节，SHA-256 为 `959e77dcb434a98cc3add57ae21d6db0aebe79256288ca3c6bb53296e268c084`。该版本文件会更新，测试每次独立获取预期内容并检查 ETag 一致，不将当次 hash 固化为未来服务数据要求。对象无 VersionId；此次只证明 AWS 匿名只读路径。

## MinerU 4.x 真实验收

在代码基线 `272f655` 上，以独立临时 venv 安装官方 PyPI `mineru==4.0.12`，wheel 为 3,682,241 字节，SHA-256 `a3ff8582a4608eb1f4dde55800ac156d59b01ceabd2ab4002bdf8e86306b1eab`。运行时及缓存均位于 `/Users/comet/.claude/jobs/d5cb2ec6/tmp/mineru4-acceptance-pngwlc5t`，没有向 V2 的 `.venv`、pyproject 或锁文件加入 MinerU。macOS 官方依赖安装了 Torch 2.15.0 / Transformers 5.21.0；本次 Flash 原生文字路径未下载推理权重，隔离 HF/ModelScope 缓存为空，实际响应 `model_used=null`。

用户明确批准安装并运行 4.0.12 后，实际启动官方 `mineru-api`，仅监听 `127.0.0.1`，使用 `--tier flash --concurrency 1 --disable-image-analysis`。显式设置独立 `MINERU_HOME`、空配置、临时目录和离线模型缓存，不读取用户私有配置。此前旧 2.6.8 包的执行拒绝没有绕过。

夹具由 `tests/fixtures/pdf.py::minimal_pdf_bytes` 独立生成，584 字节，SHA-256 `5c8c1fe71157feb854c2c4e8b4b8756e80f07c83e8516d66aa8e29491a36ab98`，文字预期为 `Hello PDF`。最终验收运行 `04347def2d45` 的 endpoint 为 `http://127.0.0.1:52072/v1/file_parse`，请求 `tier=flash`、`ocr_mode=txt`、`output_formats=markdown`；原始请求实际返回 HTTP 200、任务/文件 completed、`files[0].parse.parser_version=4.0.12`、`model_used=null` 和 `content.markdown="Hello PDF"`。

现有严格集成为 **1 passed / 0.09 秒**。适配器输出仍是一个 text Block、零 page/Asset、`partial`、`coverage=None` 和 `mineru_markdown_only`；注入 client 未被解析器关闭，输入字节未修改。测试随后关闭 client、保存 ParseResult 并删除其测试 PDF，读回内容/身份/报告不变；服务停止后的另一个 `python -I` 进程再次读回快照、投影和派生 Markdown，仍为 `Hello PDF`。正式集成测试的新增断言只验证资源交接，没有改变适配器。

验收后发送 SIGTERM。进程返回 `-15`，日志明确完成 application shutdown，端口连接被拒，服务拥有的临时目录为空。记录保留实际退出码，不把它写成 exit 0。此服务不是常驻交付组件，不保留运行进程，也不宣称服务重启、断电耐久或远端部署认证已验证。

原始输入、HTTP 响应、health、完整依赖版本、服务和 pytest 日志、ParseResult、新进程读回及关闭事实保存在本地 `tmp/mineru4-acceptance-04347def2d45/`；此前首次运行 `tmp/mineru4-acceptance-cf2ca97c7d4f/` 也保留。它们是本地证据，不打入分发包。

可复验时先在独立环境安装固定包，再启动服务：

```bash
uv venv --python 3.12 /new/external/mineru4-runtime
uv pip install --python /new/external/mineru4-runtime/bin/python 'mineru==4.0.12'
# MINERU_HOME 和 TMPDIR 选择专用目录；HF_HUB_OFFLINE 阻止自动下载推理权重。
mkdir -p /new/external/mineru4-state /new/external/mineru4-temp
MINERU_HOME=/new/external/mineru4-state TMPDIR=/new/external/mineru4-temp \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
/new/external/mineru4-runtime/bin/mineru-api \
  --host 127.0.0.1 --port 18084 --tier flash --concurrency 1 --disable-image-analysis
```

另一个终端在 V2 worktree 执行：

```bash
COMET_RAG_TEST_MINERU_URL=http://127.0.0.1:18084/v1/file_parse \
COMET_RAG_TEST_MINERU_VERSION=4.0.12 COMET_RAG_TEST_MINERU_TIER=flash \
.venv/bin/python -m pytest tests/integration/test_mineru.py -m integration -vv
```

## 未完成的真实验收

- MinerU 扫描 OCR、Basic/Standard/Advanced、多页/复杂版面识别质量、结构 JSON 和图片资源未验收。本次完成的是已实现 Markdown-only 适配器的 Flash 原生文字 PDF 路径，不能推广为整个 MinerU 能力通过。
- 兼容 S3、认证和版本对象：专用服务集成入口已有，缺配置直接失败、不 skip。自动审批拒绝执行本次选用的 `chrislusf/seaweedfs:3.85`，理由为 `[Code from External]`；未启动或以其它入口绕过。AWS 匿名只读结果不覆盖这些行为。

严格入口：

```bash
# 公开 AWS S3 匿名只读，需要外部网络，不需要凭据。
python -m pytest tests/integration/test_public_s3.py -q

# 专用兼容 S3；调用方提供 endpoint/access key/secret/region 环境变量。
python -m pytest tests/integration/test_remote_sources.py -k real_s3 -q

# 显式提供URL(完整/v1/file_parse)、实际4.x VERSION和TIER。
# flash + ocr_mode=txt 仅验证原生文字PDF，不能证明扫描OCR/VLM质量。
python -m pytest tests/integration/test_mineru.py -q
```

环境变量名称和全部运行范围见 [测试说明](../tests/README.md)。阶段 C 的首版外部引擎出口已由上述 MinerU Flash 文字 PDF 实测补齐；完成结论仅包含明确验收的路径，S3 仍只覆盖 AWS 匿名只读，未验收的模型/服务能力不计入。
