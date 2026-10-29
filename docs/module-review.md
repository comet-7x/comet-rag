# V2 逐模块审核

本轮从 `44cd6fb` 开始，以本地 `tmp/refactoring/overview.md` v1.3 和对应模块设计为依据。既有阶段验收作为历史证据；每组重新检查测试、实现、调用方和失败路径，修复以可复现输入为准。审核不自动提交或推送。

## 顺序与状态

| 顺序 | 范围                                                                    | 当前状态                                              |
| ---- | ----------------------------------------------------------------------- | ----------------------------------------------------- |
| 1    | `content.py`、`identity.py`、`_urls.py`、`_async.py` 与架构守卫 | 已审核并修复三项问题，验证见下文                      |
| 2    | sources：本地、HTTP、S3、路由和资源所有权                               | 已审核并修复，独立复审与最终检查通过；真实 S3 边界见下文 |
| 3    | documents：schema、codec、投影、解析器和导出                            | 已审核并修复四项问题；全量检查通过，真实引擎边界见下文 |
| 4    | chunkers / indexing：范围、物化、表示、关系和身份                       | 待审核                                                |
| 5    | embedder / rerankers：预算、角色、映射与实际 SDK 能力                   | 待审核                                                |
| 6    | docstores / vectorstores：事务、清单、幂等、过滤与核对                  | 待审核                                                |
| 7    | retrieval / pipelines：归并、融合、降级、入库与删除                     | 待审核                                                |
| 8    | examples / evals / 交付：真实调用、质量比较与安装边界                   | 待完整审核；本轮已复验核心 wheel                      |

每组按正确性、可读性、架构、安全与性能检查。不会将单元测试通过当作真实模型、外部服务或持久后端验收。

## 第一组：基础契约与生命周期

### 已修复

1. **凭据参数漏检。** `client_secret`、`refresh_token`、`api-key`、`q-signature` 原先可进入持久 URL，HTTP 默认身份也会随这些参数值变化。共享检查补齐已复现的参数名；大小写和百分号编码继续由既有标准库处理。来源必须提供显式稳定 ID，获取后不保存敏感 locator；文档来源、链接和媒体序列化拒绝这些 URL。普通业务 query 保持原样。
2. **架构守卫遗漏 from-import 的被导入名称。** `from comet_rag import indexing`、相对导入等可绕过既有规则。守卫补查被导入名称，保留模块检查，并增加合法类型和包相对导入的正例，避免误拒绝。SDK 守卫补充 OpenAI/Anthropic 包名；当前生产代码未发现这些违规依赖。
3. **媒体 URL 结构校验不足。** 非数字、零及超范围端口和带换行、制表符或首尾空格的 URL 原先可构造并持久化。现在在构造阶段通过标准库拒绝，合法显式端口与业务 query 原样保留；不进行 DNS、网络或文件读取。

`identity.py` 未发现需修改的问题；不改变现有身份算法、版本和已存 ID。`_async.py` 未发现需修改的问题，新增真实阻塞读取的批次主动取消测试，验证重复取消期间等待线程结束、停止派发并传播取消。

### 验证证据

- 修改前：`make install`、`make check`、`make coverage` 通过，660 项单元测试，整体覆盖率 90%；核心 wheel 在仓库外仅安装 comet-rag/lxml 的环境中完成两个隔离进程的写入与读回。
- 三项问题的新增用例在旧实现上得到 **22 failed / 116 passed**，失败分别来自凭据漏检、非法 URL 未拒绝和架构守卫漏检。
- 修复后相关五个测试文件 **143 passed**；真实回环 HTTP 集成 **2 passed / 1 deselected**，未选中需配置的 S3 用例。
- 两次内存 mutation 分别关闭停止派发和取消排干守卫，对应测试均准确失败；磁盘实现未被 mutation 改写。新增批次取消测试已独立复审。
- 最终 `make check` 通过：**688 passed**，Ruff 101 文件通过，Pyright **0 errors / 0 warnings**，wheel/sdist 构建成功。`make coverage` 同为 **688 passed / 90%**（4623 条语句，471 条未覆盖）。首次全量检查发现普通路径空格被误拒，已缩小字符限制，保留原有 Markdown 空格转义测试，并新增 DEL 拒绝用例后重跑通过。
- 修复后 wheel 在仓库外新环境 `/tmp/comet-v2-module-fixed.aXfuau/venv` 中仅安装 comet-rag/lxml，两个 `python -I` 进程完成 write/read；另直接调用安装包确认路径空格保留和新增凭据名称识别。不将临时环境作为交付依赖。

### 边界

凭据识别依赖已知参数名，无法证明任意业务自定义 URL 不包含秘密；自定义访问凭据不能写入持久定位。此次没有迁移既有快照或扫描、改写已有存储；此前已保存的敏感定位需要调用方另行处理。非法 URL 将比此前更早失败。

第一组未重新验收真实模型、PostgreSQL、Milvus、S3 或 MinerU。用户已有 `.gitignore` 修改和未跟踪资料不属于本轮改动。

## 第二组：来源与资源所有权

本轮接续已推送的 `cab5ee1`，检查 sources 的类型、路由、本地文件、HTTP/S3 获取及下载清理。HTTP SDK 行为核对当前安装的 HTTPX 0.28.1；不修改共享客户端的 hook/transport。

### 已修复

1. 本地超限改抛公共 `SourceResourceLimitExceeded`，与 HTTP/S3 一致；该异常仍属于 ValueError。验证限额恰好等于输入大小可读、超限失败且原件不变。
2. S3 指定版本时必须由响应的 VersionId 精确确认；缺失或不匹配直接失败并关闭 Body、清理下载。不再把请求版本伪装为服务端已确认版本。
3. HTTP 来源身份和非敏感定位采用客户端合并参数后的初始请求 URL，保留具有定位意义的普通 query；仍以初始入口为来源，不用重定向结果改变来源身份。
4. 同源重定向继续使用注入认证，并为新 URI 重新生成认证头；一旦跨源不恢复原客户端认证。原有跨域认证剥离测试保留。
5. 原生 Digest 握手在 `client.send()` 返回前会读取挑战响应；对认证交接的响应流施加字节预算，避免最终下载之外的无界读取。
6. 响应和认证流程关闭共用排干处理，屏蔽重复取消、等待关闭完成；保留已有下载异常，或在无下载异常时保留取消，并把清理失败附注到主异常。认证流程在 send 外的原异常上下文关闭，避免 GeneratorExit 掩盖下载错误。
7. 独立复审补齐压缩认证响应的拒绝：在 HTTPX 隐式解压缓存前要求 identity 编码，原始流预算同时约束认证挑战。新增四项先失败后修复的反例覆盖压缩、认证关闭失败/取消与取消后关闭失败。

### 全局异常基类

新增 `comet_rag.errors.CometRAGError` 并从包根公开导出。现有 SourceError、ArchiveResourceLimitExceeded、IngestionIncomplete 继承它，SourceResourceLimitExceeded 通过 SourceError 继承；保留原有 ValueError/RuntimeError 分类和异常数据。错误基类仅依赖标准库，架构守卫增加对应负例。

统一捕获仅涵盖库定义的异常。没有把参数校验、所有文件系统/SDK 失败或 CancelledError 一律包装，也没有预建其它模块的空异常类型；后续新增模块自定义异常使用同一基类。公开说明见 README 的异常小节。

### 验证与边界

- 本地超限与 S3 缺失版本用例修改前 **2 failed / 1 passed**；HTTP 新边界用例修改前 **5 failed / 1 passed**；清理错误优先级新增两项先失败后修复。
- 全局基类用例先因导出不存在失败，接入后通过；覆盖统一捕获、原有内建分类及取消不被捕获。
- 最终 `make check`：**706 passed**，Ruff 104 文件、Pyright 0 errors / 0 warnings，wheel/sdist 构建成功；`make coverage`：**706 passed / 90%**（4701 条语句、480 条未覆盖）。独立复审确认此前三项 HTTP 边界无残留，新增四个回归实例 **4 passed**；修改文件的 pre-commit（含 Bandit、敏感信息检查和 unit）全部通过。
- 真实回环 HTTP 两项与公开 AWS S3 匿名只读一项：**3 passed / 1 deselected**；随后新增第三项真实回环 Digest 校验，三项 HTTP **3 passed / 1 deselected**，实际校验 SHA-256 签名、同源重定向和挑战预算。未选择需专用配置的兼容 S3 用例。
- 最终核心 wheel 在仓库外的新环境中只安装 comet-rag/lxml，两个 `python -I` 进程 write/read 通过，并核对安装包的统一异常继承关系与实际安装依赖。wheel 的 66 个运行时文件逐字节匹配当前源码；sdist 检查 129 个成员，未包含本地资料、私有配置和运行产物。

真实 AWS 检查只证明匿名只读，不证明版本对象、认证或兼容服务；S3 缺失/不匹配版本的拒绝以离线响应验证。没有访问旧数据库、修改其它 checkout 或创建远端对象。真实模型与持久后端本轮未重验。

## 第三组：文档契约与解析保真

审核 schema、codec、投影、Markdown 导出、文本/图片/MinerU 解析器，并独立审核原生 DOCX 与 ZIP/XML 保护。schema、codec、投影、文本及图片入口未发现本轮需要修改的确定问题；保留既有身份版本和公开模型。没有为审核新增解析引擎或异常包装层。

### 已修复

1. **跨段 Word 域导致有效 DOCX 整份失败。** 复杂 TOC/超链接域可跨段落，原先每段重置栈，后续合法 end 被当成不平衡。字段状态现在在同一 story 共享，在 story 结束检查未闭合；正文、各页眉和页脚独立。TOC 保留存储显示结果并报告 partial，不计算目录或域结果。
2. **行内图片重复列表编号。** 一个列表段落的文字被图片分割后，原先每段都成为同编号 list_item。现在只有首片段承载编号，后续文字作为同一来源段落的续段保留；图片开头保留空首项以保存列表身份。阅读顺序、图片资源及来源位置保留。
3. **长 XML tail 跨读取缓冲区绕过预算。** 子元素 end 时 tail 可能未读完；原先立即清理，导致百万字符 tail 在 40,000 字符限额下仍被接受。现在保留 tail，在父元素 end 或删除前驱兄弟之前校验完整 tail，继续流式清理。ZIP 总量、元素及文本节点预算保持独立，预算按字符而非 UTF-8 字节计算。
4. **MinerU 响应关闭可被重复取消打断。** identity 响应直接遍历原始异步流，关闭集中到 finally；复用从 HTTP 迁至 `_async.close_async` 的排干逻辑，等待关闭完成并保留原始异常或取消，清理错误记录为 note。HTTP 继续使用相同行为，没有从 documents 导入 sources 适配器。

### 验证证据

- 新增用例修改前：DOCX **8 failed / 1 passed**；XML tail **3 failed**；MinerU 重复取消关闭 **4 failed**。修复后新增 **16 项**均通过。
- 四次仅内存源码 mutation 分别移除跨段字段状态、列表续段判定、XML 文本限额、取消屏蔽，对应用例均失败；磁盘实现未改写。
- 独立复核 XML tail 和 MinerU 关闭修复，新增 **7 passed**，未发现已指出路径的残留。
- 最终 `make check`：**722 passed**，Ruff 104 文件、Pyright **0 errors / 0 warnings**，wheel/sdist 构建通过。`make coverage`：**722 passed / 90%**（4706 条语句，478 条未覆盖）。
- 因共享关闭逻辑迁移，重新执行三项真实回环 HTTP 集成：**3 passed / 1 deselected**；认证/重定向/预算行为通过。专用 S3 未选择。
- 最终 wheel 的 66 个运行时文件匹配源码；仓库外新 venv 仅安装 comet-rag/lxml，两个隔离进程 write/read 通过，另从安装包实际解析 DOCX，删除原输入后图片资源、codec 读回及 Markdown 导出仍有效。本轮修改文件的 pre-commit（含 Bandit、敏感信息检查与 unit）通过。

### 边界

本轮 DOCX 用例实际生成并解析 OOXML，未用 Word 渲染器验证排版；MinerU 关闭回归使用离线流，未重启或调用真实 MinerU 服务，既有 4.0.12 Flash 验收仍是历史证据。schema/projection 版本不变，但此前解析失败或错误重复编号的 DOCX 将产生修正后的快照，调用方应在新 scope 重建并核对后切换。XML 文本预算在完整节点可用时判断，不替代 ZIP 总量限制。
