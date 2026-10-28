# 阶段 A 契约与环境核验

核验日期：2026-10-29。阶段 A 固定了内容、身份、文档快照、投影和编解码交接；阶段 B–F 的功能与真实集成仍需分别验收。

## 已实现契约

- `content.py`：纯构造的 `MediaRef`、有序 `TextPart` / `ImagePart`、`ContentItem`、内容与文本视图。保留空白、顺序及联合输入边界；构造不读取媒体。
- `identity.py`：显式 JSON 值的确定性 UTF-8 编码、带 kind/version 的 SHA-256 身份、实际文件字节哈希。拒绝非有限值、非字符串对象键和隐式对象转换。
- `documents.schemas`：不可变 blocks、富文本、代码、公式、表格、Asset、原生位置、页面、来源和解析报告。校验引用、顺序、资源、父环与表格归属；嵌套 JSON 拷贝并冻结。
- `documents.projection`：v1 文本投影和 Python 字符范围；块间换行、格间制表符是无来源的合成字符。表格子块只输出一次，不能文本化的内容保留明确 limitation。
- `documents.codec`：带标签与版本的 JSON，拒绝未知字段、版本、重复键、非法引用与错误快照身份。资源访问路径不参与快照摘要，实际内容哈希参与；已知签名和凭证参数不能持久化。

默认不保存任意对象、凭证或临时签名 URL。调用方负责 config/metadata 中的内容和资源实际可读性；已知 URL 参数检查不能识别所有形式的秘密。

## 参考行为归属

旧分支固定参考点为 `ef5e94cdeaafb836ffae03d57063911f6cae5ba1`，不整体合并旧包。

| 参考行为 | 新版归属 | 必须保留的断言 |
|---|---|---|
| `engines/offload.py` | 使用阻塞工作时的小型线程桥接 | 取消及重复取消后，线程完成才释放输入；传播原取消 |
| `engines/concurrency.py`、`test_async_batch_lifecycle.py` | 实际模型批量协调 | 有界 worker，失败停止领取，排干兄弟任务，保留原始异常 |
| `engines/chunkers/recursive.py` | `chunkers/text` | 切分时保留范围；重复内容不通过搜索反推；分隔符和空白不丢失 |
| 原生 DOCX / OMML 与来源夹具 | `documents/parsers/docx` | 样式、表格合并/缺列、公式、图片和顺序逐类保留；未映射内容报告 |
| HTTP/S3 来源与 SourcePolicy | `sources` | 实际字节限额、逐跳准入、借用/临时文件所有权、完整下载 |
| 模型 wire/media 适配 | `embedder/models`、`rerankers/models` | 角色、预算、乱序/重复/缺失 index、媒体实际输入及客户端所有权 |
| DocumentStore PostgreSQL | `docstores` | 单文档真实事务、自包含内容、实际表示、完整 manifest 和嵌套隔离 |
| Milvus / 过滤 | `vectorstores` | 非破坏性 prepare、范围/空间验证、截断前过滤、明确 ID 核对与删除 |
| BM25、RRF 和降级 | `retrieval`、`pipelines` | keyword 不嵌入，先 unit 归并再融合，原始评分证据，取消不降级 |

ZIP/XML 资源预算与基础 RRF 已在阶段 0 迁入。以上其他归属是后续实现要求，不能以本表代替功能测试。

## 已运行验证

- `make check`：Ruff、格式、Pyright、107 项单元测试、wheel 与 sdist 均通过。
- `make coverage`：107 项通过，当前已实现代码覆盖率 95%。该数字不代表完整 RAG 功能。
- 仓库外建立只安装 wheel 和 lxml 的环境，以 `python -I` 运行真实 UTF-8 文件与持久媒体引用的 JSON 写入/读取。两个全新 Python 进程得到相同身份、原样正文和可读媒体；不依赖开发包或仓库导入。
- 来源夹具生成 DOCX/PDF 原始字节仍通过；不是 parser 功能验收。
- `docker compose config --quiet` 通过。专用 `comet-rag-v2` 项目中的 PostgreSQL 17.11、etcd 3.5.16、Milvus 2.6.23 均健康；PostgreSQL 实际返回版本，Milvus `/healthz` 返回 `OK`。

Milvus 使用官方 Standalone 本地存储与 Woodpecker 本地 WAL，详见[开发环境](development.md)。先前 MinIO 镜像拉取失败；采用官方支持的本地存储后已解除 Milvus 启动阻塞，未删除任何数据卷。

## 未验收部署

公开旧示例中的本机模型端口 8000、8001、8080、8081 均连接拒绝；MinerU 8989 和另记的 28995、28989 也未建立可用部署。没有读取私有配置、环境凭证或登录注册表。

阶段 A 的出口允许未知部署明确记录。真实 embedding、rerank、MinerU 和 S3 服务验证仍是后续对应阶段的必要出口；健康检查不能替代 PostgreSQL 内容事务、Milvus BM25/持久回读或真实跨模态质量验收。
