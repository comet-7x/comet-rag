# V2 逐模块审核

本轮从 `44cd6fb` 开始，以本地 `tmp/refactoring/overview.md` v1.3 和对应模块设计为依据。既有阶段验收作为历史证据；每组重新检查测试、实现、调用方和失败路径，修复以可复现输入为准。审核不自动提交或推送。

## 顺序与状态

| 顺序 | 范围                                                                    | 当前状态                                              |
| ---- | ----------------------------------------------------------------------- | ----------------------------------------------------- |
| 1    | `content.py`、`identity.py`、`_urls.py`、`_async.py` 与架构守卫 | 已审核并修复三项问题，验证见下文                      |
| 2    | sources：本地、HTTP、S3、路由和资源所有权                               | 已审核并修复，独立复审与最终检查通过；真实 S3 边界见下文 |
| 3    | documents：schema、codec、投影、解析器和导出                            | 已审核并修复四项问题；全量检查通过，真实引擎边界见下文 |
| 4    | chunkers / indexing：范围、物化、表示、关系和身份                       | 已审核并修复图片出现丢失；全量检查通过，边界见下文     |
| 5    | embedder / rerankers：预算、角色、映射与实际 SDK 能力                   | 已审核并修复；远程文本模型已小样本校准，本地模型未重验 |
| 6    | docstores / vectorstores：事务、清单、幂等、过滤与核对                  | 代码与本地检查通过；新版真实 PostgreSQL 已验，Milvus 写入被审批拦截 |
| 7    | retrieval / pipelines：归并、融合、降级、入库与删除                     | 已审核并修复上下文过滤；真实 PG 读回验证通过，完整跨后端待验 |
| 8    | examples / evals / 交付：真实调用、质量比较与安装边界                   | 已审核修复示例；真实本地模型评估复验通过，跨后端示例待重验 |

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

## 第四组：切分范围与索引物化

核对递归字符切分、跨 Block 来源映射、原子元素预算、flat/parent-child builder、类型及 codec。165 项既有切分/索引/可选检索用例通过；另用固定随机种子生成 10,000 组文本、长度和 overlap，核对字符范围、硬预算及完整覆盖，未发现递归算法需要修改的问题。父子范围唯一包含与跨界拒绝保留既有实现。

### 已修复

表格内两个 ImageBlock 引用同一 Asset 时，旧 builder 对 Asset 去重后直接用于实际输入，导致两个图片出现只物化一份 ImagePart。现在实际图片序列保留每次引用，Asset 及其来源资源独立去重；media/joint/separate 都保留重复出现，指纹按有序 parts 计算。builder 配置改为 `flat_v3`，避免修正前后策略共用 profile；单元/表示 codec 版本不变。

### 验证与边界

- 新增三种表示策略用例修改前 **3 failed**，修复后通过，覆盖单元、表示及 codec 往返；新增 profile 版本隔离断言。内存 mutation 恢复图片去重后三项均失败，磁盘未改写。
- 独立复核此修复及输入指纹，未发现确定残留，定向 **3 passed**。
- `make check`：**725 passed**，Ruff 104 文件、Pyright **0 errors / 0 warnings**，wheel/sdist 构建通过；`make coverage`：**725 passed / 90%**（4706 条语句，474 条未覆盖）。
- 最终核心 wheel 在仓库外仅安装 comet-rag/lxml，两个隔离进程 write/read 通过，66 个运行时文件匹配源码；修改文件的 pre-commit（含 Bandit、敏感信息检查、unit）通过。

修复会改变 profile，旧 scope 需重建后由调用方切换。重复图片导致的实际模型输入和表示数量增加是保真结果；joint 若超模型预算或不支持多图片仍由适配器明确拒绝。本轮未运行真实模型或 PostgreSQL/Milvus，不将纯算法审核作为后端或检索质量验收。

## 第五组：嵌入与重排适配器

核对空间身份、角色、模态、预算、输出映射、本地加载和批次生命周期。远程适配器继续只支持文本 embeddings；本地 CLIP 只接受文字或单图片；cross encoder 逐对输出单 logit。未新增依赖、同步接口、模型能力包装或通用异常层。

### 已修复

1. **CLIP 忽略角色前缀。** 空间 ID 已记录 query/document 前缀，实际 processor 却只收到原文。现在每次调用取局部前缀，文本在 tokenize 与预算检查前附加；图片不附加文字。并发两角色保留各自前缀，原文空白不变。
2. **CLIP 静默丢弃多余输出。** 单条输入若返回两行，旧代码直接取首行；零行则抛 IndexError。现在明确要求二维、恰好一行，维度和有限值仍由既有校验负责，不填补或截取结果。
3. **本地模型取消后仍派发剩余工作。** CLIP 整批和 reranker 整组推理原先各放进一次阻塞线程，取消只在所有条目/批次完成后传播。现在 CLIP 每条、reranker 每批各自 await run_sync，排干当前工作后传播取消，不派发剩余项。reranker 仍先对完整集合 tokenize/检查预算，再开始任何推理；tokenizer、pad 与模型共用原有锁，保持跨调用互斥及稳定同分排序。
4. **远程响应的预算与关闭边界不足。** 明确请求 identity，拒绝压缩，检查声明长度与实际原始流预算；最终关闭复用 close_async，重复取消不打断排干，未取消时原始 HTTP 错误优先于关闭错误。整个批次被调用方取消时仍按批次协调契约传播取消。共享客户端保持由创建者关闭。
5. **独立复核发现并修正 response hook 兼容回归。** 直接迭代原始流会重读 hook 已消费的真实网络流。现在对已消费且有缓存的正文核验预算并读取缓存；未缓存流使用原始读取和统一 finally。真实 HTTPX 回环用例覆盖普通客户端和 await response.aread() 的 hook。

### 验证证据

- 最初新增 CLIP/HTTP 回归在旧代码上 **10 failed / 32 passed**；CLIP 多条取消新增用例因实际执行 2 条而期望 1 条失败。reranker 多批取消正常完成参数修改前执行 3 批而期望 1 批，**1 failed / 1 passed**（当前批失败参数本就停止派发）。
- hook 兼容回归以真实回环网络验证，修正前 **1 failed / 1 passed**，修正后 **2 passed**。同时验证实际 POST 的前缀、乱序 index、声明/无声明长度预算及压缩拒绝；回环服务不是真实模型。
- 三次仅内存 mutation 分别移除 CLIP 前缀、输出行数检查及 HTTP 压缩拒绝，对应回归均失败，磁盘源码未改写。HTTP 新增 sibling 失败回归确认停止第三批、等待正在关闭的第二批后传播首批错误。
- 最终 `make check`：**745 passed**，Ruff **106 文件**、Pyright **0 errors / 0 warnings**，wheel/sdist 构建通过。`make coverage`：**745 passed / 90%**（4736 条语句，475 条未覆盖）。本轮比上一组新增 20 项单元测试及 2 项显式回环集成实例。
- 核心 wheel 在仓库外的新环境仅安装 comet-rag/lxml，以两个隔离进程完成 write/read；安装验证脚本首次误用参数后已纠正为 --output-dir 并通过。最终包复验核对 66 个运行时文件匹配源码，sdist 排除本地资料和运行产物。

### 边界

本轮环境没有 torch/transformers，未运行本地真实权重、tokenizer 或硬件推理；既有 CLIP 与持久检索记录仍是历史证据。后续使用用户提供的远程服务完成下述小样本校准，不据此宣称跨模态或排序质量验收。OpenAI 文本入口的 max_chars 是字符预算，不等于服务端 token 限额；本轮未增加 tokenizer 或维度缩减参数。

非空 CLIP 前缀此前未生效，已有向量须在新 scope 重建；不改变 space_id 编码，空前缀下正常向量语义不变。远程服务必须遵守 identity 响应；注入客户端的 auth/hook 在 send 返回前自行读取正文，其资源消耗由调用方负责，适配器预算不覆盖这些提前读取。核心安装验证不等同于可选模型 SDK 验收。

### 用户提供服务后的真实校准（2026-10-29）

仅发送自建猫/狗/数据库文字，不持久化地址或凭据，不读取用户配置文件；Milvus 仅做元数据只读检查，没有创建集合或写入记录。

- **Embedding：** 服务自报 vLLM **0.21.0**，模型 `Qwen/Qwen3-VL-Embedding-8B`。`/v1/embeddings` 返回 identity、4096 维有限向量；单例观测 L2 norm 为 **1.0000000732**，这不替代归一化配置保证。实际调用当前 `AsyncOpenAITextEmbedder`，三文档分为两批，并发处理 query/document，结果 ID 顺序、空间 ID 和借用客户端保持开放通过。查询 “Which animal is resting on a couch?” 对猫/狗/数据库的 cosine 为 **0.752065 / 0.336721 / 0.262441**；适配器调用部分约 **0.057 秒**，不作性能基准。
- **Reranker：** 同为 vLLM **0.21.0**，模型 `Qwen/Qwen3-VL-Reranker-8B`。真实 `/v1/rerank` 三候选全部 index 精确覆盖、分数有限；猫/狗/数据库分数为 **0.180665 / 0.008699 / 0.007165**，请求约 **0.047 秒**。这是直接 HTTP 协议校准；当前库仅实现本地 cross encoder，尚无该远程重排适配器，不冒称库内接入完成。
- **Milvus：** 通过当前 worktree 的 `AsyncMilvusClient` 成功读取用户指定数据库的集合清单；未检查或修改已有集合结构与数据，不算持久后端契约验收。
- **MinerU：** 所给服务 OpenAPI 是 vLLM 推理 API，模型为 `opendatalab/MinerU2.5-Pro-2605-1.2B`，有 chat/completions，缺少当前 `AsyncMinerUParser` 需要的 `/v1/file_parse`。不能直接把此模型 endpoint 当作文档解析服务。

上述小样本不验证图片或联合图文输入、模型 checkpoint 不可变身份、长输入/token 边界、真实取消、检索泛化或质量收益。MinIO 和普通 LLM 不在本组验证范围，未访问；后续存储审核仍需独立 scope 和完整写入/读回证据。

## 第六组：存储契约与读回一致性

审核 DocStore 的快照/清单、完整批次校验、事务与删除，以及 VectorStore 的范围绑定、不可变记录、原生过滤、确认和明确 ID 核对。修改前存储定向单元测试 **103 passed**。此组代码验证与真实后端验收分别记录；提交与推送由用户明确要求后执行。

### 已修复

1. **DocStore 合法但损坏的快照可能保持原 ID。** 表格 blocks 正文、metadata 或未影响 representation_id 的资源结构被修改后，原读回只检查 ID、行身份和父子关系，无法检出；完整清单摘要只在整批重试时核对。现在复用现有 content_digest，为每个 unit/representation 独立保存摘要。PostgreSQL 在 JSON payload 存储封套中加入 `snapshot_version=1` 和 `snapshot_fingerprint`，解码前提取封套并逐快照核验；memory 在写入时独立保存摘要，读取单元/祖先/表示时校验，删除时同步清理。不会在读取时为已有内容重新生成可信摘要。
2. **内存分数溢出。** 有限的大向量可使 IP 返回 inf，或使 L2 直接抛底层 OverflowError；现在统一拒绝超出有限分数范围的计算，不返回伪造或截断的分数。普通 IP/L2 和原有缩放 cosine 保持不变。
3. **Milvus 响应分数与重复命中校验不足。** bool/string 原先可经 float() 成为分数，同一 record_id 可重复返回。现在拒绝非数值、非有限分数和重复命中，dense/BM25 共用该校验，避免重复证据进入后续排名。
4. Milvus 集成增加 `COMET_MILVUS_DB`，默认仍为 default，重建客户端时保留数据库选择；地址与凭据未写入仓库。

### 验证证据

- 快照封套首批 23 个新增用例在旧实现上失败，另补一个 memory 实际表示损坏回归；最终 docstores + snapshot 定向 **66 passed**。PG 用例先由原 codec 解码确认破坏后 ID 不变，再断言新读取检查拒绝，避免把原身份守卫当作新摘要验证。
- 向量新增六项回归修改前 **6 failed**；修复后 vectorstores **67 passed**，包括 dense/BM25 非数值分数与重复命中共同拒绝。三次内存 mutation 移除 IP 有限值、Milvus 类型及去重守卫，对应回归均失败。
- 快照两次有效内存 mutation 禁用实际导入模块的摘要检查，memory **6 failed**、PostgreSQL 离线解码 **2 failed**。首次重建 module 的注入未作用于包导出的旧类，已识别为无效实验，不当作覆盖缺口或验证成功。
- 最终 `make check`：**775 passed**，Ruff **107 文件**、Pyright **0 errors / 0 warnings**，wheel/sdist 构建通过。`make coverage`：**775 passed / 91%**（4775 条语句，426 条未覆盖）。修改文件的 pre-commit（含 Bandit、敏感信息检查、unit）通过。
- 最终 wheel 在仓库外新环境仅安装 comet-rag/lxml，两个隔离进程完成 write/read；66 个运行时文件匹配源码，sdist 132 个成员排除本地资料与运行产物。核心安装不加载可选数据库/模型 SDK，不能代替真实后端验收。

### 存储格式与未完成验收

公共 IndexUnit/Representation codec、manifest v2 和 SQL 表结构不变；**DocStore JSON 存储封套升级**。此前无快照摘要的旧 scope 会明确拒绝读取/重试，应在新 scope 重建、验证后由调用方切换，不能混用新旧 writer。单 ID 读取不加载整篇文档，合法部分删除后剩余单元继续可读且仍校验摘要。摘要沿用既有 Asset media 访问位置/name 可迁移语义，不防御同时改写正文和摘要的操作者。

损坏内容的强制清理没有加入公开接口：PostgreSQL 仍在删除前解码快照并检查关系，损坏时失败关闭，清单保留；需要专门的恢复策略，不能为清理绕过父子/范围保护。

本轮新建独立 PostgreSQL 17 实例，只绑定回环随机端口、使用临时随机密码和独立内存数据目录，不复用已有容器或资料。新版六项真实 PostgreSQL 集成 **6 passed / 0.59 秒**，覆盖封套读回、身份不变的正文/metadata/资源损坏拒绝、事务回滚、并发、重连和精确删除。实例保留，内存数据目录不提供容器停止后的持久性；该证据不代表重启/断电耐久。首次 trust 免认证启动被自动审批拒绝，改用密码认证后通过。Milvus 集成写入被自动审批拒绝，要求明确批准共享数据库修改；未换工具或目标绕过。准备执行的两个用例每次生成三个独立 UUID scope/集合，共四条逻辑记录及幂等重试；创建索引/加载集合、dense/BM25/过滤/不兼容准备/客户端重连检查后，仅按本轮明确 record IDs 删除，保留新集合，不操作既有集合、不删数据库、不重启服务。此前只读连通不能作为本组持久验收通过。

## 第七组：检索与流水线

测试先行检查单通道排序、多表示归并、RRF、重排失败回退、父上下文与预算，再核对入库重放和精确删除。入库继续在 resolver 上下文内完成模型消费，先提交完整内容/实际表示/向量清单，再写向量并核对明确 IDs；删除失败保留清单，不删除共享资产。本轮未发现需要修改入库与删除实现的确定问题。

### 已修复

**父上下文扩展可能越过 metadata 过滤。** 合法父子快照可带不同 metadata，向量召回 public 子单元后，旧流程直接返回 restricted 父正文。现在复用既有过滤匹配函数，读回候选先于重排核对过滤；扩展后的上下文先于去重和预算检查同一过滤。Eq/In/And 和系统字段使用原有精确类型语义，失败直接传播，不回退到扩大范围的内容。请求仅允许某个 child unit_id 时，扩展到其他父 ID 也会失败；调用方可明确关闭扩展。

### 验证证据与边界

- 使用真实 memory stores、完整合法清单和向量复现，新增回归在旧实现 **9 failed / 1 passed**；修复后新增 **10 passed**，相关四组 **111 passed**。覆盖 Eq/In/And、unit_id、零预算不能掩盖过滤错误、合法父扩展与子证据保留、重排前拒绝不符过滤的读回内容。
- 两次仅内存 mutation 分别禁用上下文与候选匹配，回归分别 **8 failed / 1 failed**，磁盘源码未改写。
- 新增真实 PostgreSQL 父子快照读取与过滤用例，和原六项 PG、两项真实 HTTPX 回环集成合跑 **9 passed / 1.71 秒**。该搜索用例的召回使用 memory vectorstore，真实 SQL 读回不等于真实 Milvus 搜索。
- `make check`：**785 passed**，Ruff **108 文件**、Pyright **0 errors / 0 warnings**，wheel/sdist 构建通过；`make coverage`：**785 passed / 91%**（4781 条语句，426 条未覆盖）。
- 最终 wheel 在仓库外新建环境仅安装 comet-rag/lxml，两个 `python -I` 进程完成 write/read，无可选 SDK 导入；66 个运行时文件与源码一致，sdist 133 个成员排除本地资料与运行产物。提交前 Bandit、敏感信息检查、lint、unit 钩子通过。
- 本轮没有运行包含新版封套、真实远程 embedding 和真实 Milvus 的完整入库/检索链；共享 Milvus 写入审批仍阻塞，不能用本组局部验证代替。

## 第八组：示例、评估与交付

### 已修复

两个持久示例创建 engine 后，Milvus client 或 Store 构造失败会绕过旧 finally；engine.dispose 失败也会跳过 client.close。现在创建后立即登记到标准库 AsyncExitStack，逆序关闭并复用 close_async 排干，原始准备/取消错误保持优先，关闭错误附注。两个入口增加 `--milvus-db`（默认 default），ingest/verify 使用同一显式数据库，不再只能连接默认库。未引入新包装层或依赖。

### 验证证据

- 新增 12 项示例回归在修改前全部失败，修复后 **12 passed**，覆盖客户端构造失败、prepare 失败、关闭失败、取消，以及默认/指定数据库的真实 argparse 入口。禁用关闭函数的内存 mutation 导致 **8 failed**。三个示例/评估 CLI 的 `--help` 实际运行通过。
- 当前 worktree 安装锁定 local-models extra，复用本地固定权重和图片运行真实 CPU CLIP/MiniLM；12 文档/8 文字查询、两图片及 text/media/separate/joint 完整执行，未访问模型网络。权重指纹与示例文档一致。Flat MRR/Hit@1 为 **1/1**；parent-child 无重排为 **0.9375/0.875**，重排后为 **1/1**；不更改默认策略。
- 独立从原始命中与标签重算 **44 行**逐查询指标及组均值，全部一致；三组图片指标为 1，joint 和图片候选重排明确 unsupported。报告保存在本地 `tmp/eval-module-review-20261029.json`，不打入分发包；细节见 [evals](../evals/README.md)。两图同图查询不证明跨域泛化，也不计作持久后端验收。
- `make check`：**797 passed**，Ruff **109 文件**、Pyright **0 errors / 0 warnings**，wheel/sdist 构建通过；`make coverage`：**797 passed / 91%**（4781 条语句，426 条未覆盖）。示例修改的 pre-commit 含 Bandit、敏感信息扫描与 unit 全部通过。核心运行代码未在本组修改，上一组仓库外核心 wheel 实际调用证据仍适用；最终分发另核对当前示例/文档与本地资料排除。

### 尚未完成

完整新版 PostgreSQL/Milvus 持久示例仍未重跑，共享 Milvus 写入审批未解除；本轮也未重启持久服务。真实模型取消/并发压力、扫描 OCR 和领域质量未验收。审核与本地修复完成不代表这些外部出口已经通过。
