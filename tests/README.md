# V2 测试

默认 pytest 只运行 `tests/unit`，不访问真实网络、模型、GPU 或数据库。真实 SDK/服务检查显式选择 integration；选中缺配置/后端时直接失败，没有全局 skip。测试默认使用 importlib 收集，unit/integration 同名文件可一同执行。

```bash
make install
make check
make coverage
make pre-commit
```

单元测试包括身份/codec 错误输入、自包含结构、ZIP/XML/图片/流式预算、来源准入与清理、线程取消排干、模型身份映射、过滤、manifest、unit 归并/RRF 和重排/父上下文。一致性守卫有对应错误输入；架构导入守卫也注入非法 import 证明会失败。DOCX/PDF 来源夹具保留可读生成代码，不用旧 parser JSON 快照作新契约。

## 显式真实集成

```bash
# 提前给定专用测试 PostgreSQL URL；不要连接旧资料数据库。
python -m pytest tests/integration/test_postgres.py -q

# COMET_MILVUS_URI 可覆盖默认回环地址 29530；每次使用 UUID scope。
python -m pytest tests/integration/test_milvus.py -q

# 真实回环 HTTP server，不需要外部网络。
python -m pytest tests/integration/test_remote_sources.py -k 'not real_s3' -q

# 公开 AWS S3 匿名只读，联网但不需要账户或凭据。
python -m pytest tests/integration/test_public_s3.py -q

# 专用兼容 S3 服务，不会猜测用户配置。
python -m pytest tests/integration/test_remote_sources.py -k real_s3 -q

# 明确部署的 MinerU 4.x /v1/file_parse；显式声明实际版本与tier。
python -m pytest tests/integration/test_mineru.py -q
```

| 用例 | 显式配置 |
|---|---|
| PostgreSQL | `COMET_RAG_TEST_POSTGRES_URL`，SQLAlchemy asyncpg URL |
| Milvus | `COMET_MILVUS_URI`，默认 `http://127.0.0.1:29530` |
| 公开 AWS S3 | 无环境配置；固定 NOAA 小型公开对象，禁用 AWS 配置和凭据查找，只读 |
| 专用兼容 S3 | `COMET_RAG_TEST_S3_ENDPOINT`、`COMET_RAG_TEST_S3_ACCESS_KEY`、`COMET_RAG_TEST_S3_SECRET_KEY`、`COMET_RAG_TEST_S3_REGION` |
| MinerU | `COMET_RAG_TEST_MINERU_URL`，完整 `/v1/file_parse` endpoint；`COMET_RAG_TEST_MINERU_VERSION`（如 `4.0.12`）、`COMET_RAG_TEST_MINERU_TIER`（如 `flash`） |

PostgreSQL 测试在专用 `comet_docstore_v2` schema 中使用 UUID scope，验证 JSON 原样保留向量/metadata 负零，模拟最终 manifest INSERT 故障验证真实回滚，检查自包含内容/实际表示、确认、重连、冲突和删除。Milvus 测试使用独立 scope 集合，prepare 失败不 drop，测试清理只删自己明确 records，集合保留。测试不会重启容器；函数中的 restart 指重新建立客户端，服务重启证据另由实际示例记录。

公开 S3 用例匿名只读 NOAA GHCN 的小型版本文件，通过独立 HTTPS 字节和 ETag 对照 SDK，验证实际解析、SHA/元信息/来源身份、超限拒绝、下载清理与借用客户端；不读取本机 AWS 配置或凭据，不创建/删除远端数据。公开文件会更新，ETag 不一致时测试失败，不能冒用旧 hash。该用例不覆盖认证、VersionId 或兼容服务。

专用兼容 S3 测试创建本次 UUID bucket/object，仅清理自己的对象。MinerU 测试上传可读代码生成的原生文字 PDF，要求实际提取 Hello PDF、核对引擎版本并返回明确 partial Markdown；关闭客户端和删除输入后，再从 codec 快照读回相同内容/报告/身份。已在独立 MinerU 4.0.12 Flash/txt 服务通过，详见 [阶段 C](../docs/phase-c.md)。不因此声称扫描 OCR、其它 tier、原生结构或图片资源已验收。缺少部署时不执行对应集成，记录未验收，不能用 mock 替代。

CI 实际启动自己 runner 的 PostgreSQL/Milvus，执行六项 PG、两项 Milvus 和三项 HTTP 用例（含实际校验 Digest 签名、同源重定向和挑战预算）；该 job 不包含 S3/MinerU，也不伪称 GPU 或真实模型 job。

## 核心安装与真实模型

`uv build` 后，在仓库外建立只安装 wheel 的环境（只有 comet-rag 和 lxml），用独立解释器执行：

```bash
python -I /absolute/path/to/scripts/check_core_install.py --output-dir /new/external/roundtrip --phase write
python -I /absolute/path/to/scripts/check_core_install.py --output-dir /new/external/roundtrip --phase read
```

脚本实际解析 UTF-8/CRLF 文件、跨进程 JSON 读回、Markdown 派生、自包含索引内容和无模型 keyword 检索，核对身份与无可选 SDK 导入。手工二维向量仅用于核心存储/编排检查，不是模型验收。

真实模型和持久流程在 [examples](../examples/README.md) 中执行，策略质量/成本在 [evals](../evals/README.md)。它们固定本地权重与图片指纹，保存原始证据，默认单元测试不会下载或加载模型。四种模态组合、临时文件释放、新进程重连与真正服务重启分别记录。

本轮实际结果与未验收项见 [validation](../docs/validation.md)。单元覆盖率包含未在 unit 中运行的真实 PostgreSQL 适配器，百分比不代表完整系统验收。
