# V2 验证记录

核验日期：2026-10-29。所有源码修改与工具运行针对 `refactor/comet-rag-v2` 当前 worktree；没有推送，没有迁移旧数据库或删除既有卷、集合、资料和私有配置。阶段 0/A 的历史证据保留，当前实现不能仅凭那些检查算通过。

## 当前出口

| 阶段 | 实际状态 |
|---|---|
| 0 / A | 已完成并提交，见 [0](phase0.md) / [A](phase-a.md) |
| B | 本地 TXT/DOCX→真实模型→PostgreSQL/Milvus→三模式通过；本轮向量重放/原生异步 SDK 修订后已用新 scope 验证新进程读回，服务重启证据属于此前版本，见 [B](phase-b.md) |
| C | 本地/HTTP/原生 DOCX/公开 AWS S3 匿名只读已验证；MinerU 实际部署待验收，兼容 S3 等范围未验证，见 [C](phase-c.md) |
| D | HTTP/文档图片、四种组合与新进程读回通过；此前版本另有真正服务重启证据，本轮修订未再次重启，见 [D](phase-d.md) |
| E | 父子/重排完整性、实际模型质量与成本比较通过，默认策略未启用，见 [E](phase-e.md) |
| F | 导出、示例/评估、构建白名单、架构守卫与 CI 已实现；核心和来源/图片 wheel 外部调用通过，最终全阶段验收仍依赖 C |

## 本轮严格审查与修复

先核对需求、库内已有实现、标准库能力、上游 SDK 原生支持和最简实现，再对调用方、测试及持久行为作交叉复审。修复以下已复现问题：

- MinerU 旧版本选择依据不足：改为官方 4.x `/v1/file_parse`，核对官方异步 SDK 后保留轻量 HTTP 边界；严格校验实际 parser version、任务/文件状态、上传与响应预算，202 不重传，清理失败不覆盖原始错误。实际服务仍未验收。
- Milvus 以同步 SDK 加线程实现异步：改注入原生 `AsyncMilvusClient`，13 个网络方法直接 await，示例和集成同步迁移，借用客户端由创建者 await close。
- 批量任务立即失败时其它 worker 继续派发：在失败 worker 中立即停止派发，保留已启动工作取消和排干。
- 持久 URL 凭据规则遗漏：复用同一纯标准库检查，补齐 auth、credential、空值及编码参数名，仍允许文档业务 query 中的 `key`。
- 重试再次调用模型，浮点微差造成永久清单冲突：manifest v2 在 PostgreSQL 内容事务保存首次实际向量，重试直接重放并核指纹。真实 PostgreSQL JSONB 会正规化负零，payload 因而改用原生 JSON；新默认 schema `comet_docstore_v2`，不覆盖旧表。
- 每个文本 chunk 重复保存完整来源 block：按原始范围物化文本/代码并保留样式、链接和坐标，表格/公式继续保持完整；profile 升级 `flat_v2`。20,000 字符/200 units 序列化从 4,228,870 降至 248,870 字节，10,000 字符为 124,270 字节。
- CLIP 空间指纹遗漏分片和附加处理文件：覆盖 safetensors 索引与全部实际分片、added_tokens 和 processor_config；缺失/非法分片在模型加载前拒绝。

各守卫以对应错误输入和独立预期验证；实现完成后交叉只读复审未发现上述修订的新阻断项。这是本轮已核对范围的结论，不保证所有未知缺陷均已排除。

当前实现已执行 `make install`、`make check` 和 `make coverage`，全部通过：**660 passed，Ruff 101 文件，Pyright 0 errors / 0 warnings，覆盖率 90%（4620 条语句、471 条未覆盖）**。全量 pre-commit 及新增 `_urls.py` 的显式检查通过，包括 Bandit、detect-secrets、Ruff 和单元；Compose 与 diff 空白检查通过。wheel/sdist 已成功构建。

真实验证结果：

- 原生异步 Milvus 两项集成通过，60.90 秒，覆盖 dense/BM25、过滤、schema/space 冲突保留数据、明确 ID/指纹和重连读回。测试仅删除本次 records，集合保留。
- 新 PostgreSQL 容器 `comet-rag-v2-review-043f2924bc71`，回环端口 57063：**6 passed / 0.59 秒**，包含向量与 metadata 负零在 dispose/reconnect 后身份和重放不变。首次运行因测试仍断言旧 schema 名而失败，修正为新 schema 后完整六项通过；未删除失败用例。
- 文本 scope `review-text-043f2924bc71`：四文档实际 CLIP→PostgreSQL/Milvus，dense/keyword/hybrid、同清单重试和新进程 verify 全部通过。进程用时为 81.84 / 3.17 秒。
- 图片 scope `review-multimodal-043f2924bc71`：五文档、猫/狗四种组合共八次查询、HTTP 临时下载释放、DOCX 内嵌图，以及明确 IDs/指纹/实际表示/Asset SHA 在新进程复验全部通过；示例内部计时 51.46 / 2.36 秒。
- 本轮模型/持久证据为 `tmp/review-validation-043f2924bc71.json` 与上述 scope 目录，使用 Milvus `127.0.0.1:54150` 的新集合。没有再次重启服务；下方 `925d906` 的真实重启记录只证明当时版本。
- 新 wheel 在仓库外独立环境 `review-wheels-afe4d7f9911d-ry7etp82` 中验收。核心环境仅 comet-rag+lxml 6.1.3，来源/图片环境仅 `[sources,images]` 和其依赖；各自两个 `python -I` 进程完成实际文件解析/codec/索引/keyword，或真实 HTTP 图片/原生 DOCX/Markdown/Asset SHA/清理检查。65 个运行文件与源码逐字节一致，分发包白名单检查通过，证据为 `tmp/review-wheels-afe4d7f9911d.json`。文档完善后再次重建并在 `review-wheels-f5f563bd6ac6-mp4viqbs` 的两个新独立环境重复全部安装调用，均通过，证据为 `tmp/review-wheels-f5f563bd6ac6.json`。后续变更仅补充本记录；最终包继续核对源码、已验收运行文件和分发白名单。

`pymilvus` 2.6.17 首次握手取消时，handler 尚未注册、client 尚未保存引用，调用 close 仍可能无法释放。已用真实 SDK 和本地假连接策略复现；已建立连接的 RPC 取消通过测试。没有使用私有 SDK 清理包装，也不能宣称该上游问题已解决。建议应用初始化时完成 prepare，见 [架构边界](architecture.md)。

## 此前版本的可复验证据

- PostgreSQL 五项真实集成通过，最终一次为 0.45 秒。包含真正事务回滚故障、持久快照/实际表示/清单、范围隔离、确认并发、损坏拒绝和明确删除。
- Milvus 两项 + 实际 HTTP 两项组合通过，64.21 秒，另有一个 S3 用例显式未选中。向量测试核对明确 IDs/指纹，过滤在 limit 前执行；没有拿 count 或健康状态代替内容。
- 文本 `phase-b-24f96d492310`：四文档实际 CLIP 512 维入库，dense/keyword/hybrid 均命中预期，最新身份规则下新进程复验通过。
- 图片 `phase-d-24f96d492310`：在表示资产守卫加强后的最终代码上，五文档实际入库，猫/狗分别四种模态共八次检索，HTTP 临时下载释放，DOCX 图像可检索，新进程逐 ID/资产 hash 复验通过。
- 图片真正服务重启：代码 `925d906`、本轮全新专用项目 `comet-rag-v2-acceptance-47e151d5d11b`、scope `phase-d-restart-47e151d5d11b`。五文档入库后显式重启 PostgreSQL/Milvus（exit 0），容器 ID/Created 不变、StartedAt 更新；全新进程核对完整 manifest、明确 records/指纹、unit/实际表示/图片 SHA，以及八次查询和 DOCX 内嵌图全部通过。证据 `tmp/acceptance-restart-47e151d5d11b.json`，详细时间见 [D](phase-d.md)。这关闭本轮图片重启出口，不依赖更早的 B 重启记录。
- 公开 AWS S3：`tests/integration/test_public_s3.py`，1 passed / 14.75 秒。通过匿名 SDK 读取 NOAA 小型对象，与独立 HTTPS 的字节/ETag 对照，验证 SHA/元信息/稳定身份/实际解析/清理/超限拒绝/客户端所有权。只证明 AWS 匿名只读，不覆盖兼容服务、认证和 VersionId。
- 真实 CLIP/MiniLM 评估在 `tmp/eval-phase-e-final.json`，8 查询/12 文档及两个图片，完整输入/标签/分数/时间/文件指纹；有限结果见 [evals](../evals/README.md)。不是生产基准。
- 核心 wheel 在仓库外新建环境，仅 comet-rag + lxml 6.1.3；`python -I` 两个独立进程实际解析文件、持久 JSON、索引重建与 keyword 检索，得到相同 document/unit IDs，未加载可选 SDK。wheel/sdist 内容检查排除 tmp、模型、凭据与清理备份，并包含 examples/evals/scripts。

新增 S3 用例和本轮验收文档后，再次执行 `make install`、`make check`、`make coverage` 全部通过：606 项单元测试通过，100 个 Python 文件 Ruff 检查/格式通过，Pyright 零错误，wheel/sdist 构建成功。单元覆盖率为 90%（4536 条语句，470 条未覆盖），包含未在 unit 中连接真实数据库的 PostgreSQL 适配器；不按覆盖率或测试数量推导功能验收。

提交钩子对全部已跟踪及新增交付文件通过，包括 Ruff、Bandit、detect-secrets 与全部单元测试。仅逐行标注固定 Python 子进程、公开模型 revision 和无效凭据 URL 反例的已核对误报；未扩大扫描忽略范围。无适用文件的 symlink/JSON/YAML 排序钩子正常不执行。Compose 配置和 diff 空白检查通过；远端 GitHub Actions 未运行。

本轮重建 wheel/sdist 后，新的核心安装环境为任务临时目录 `core-install-nvducqs9/venv`，仅包含 comet-rag 0.2.0.dev0 与 lxml 6.1.3。两个独立进程核对文档/索引身份、原始空白、Markdown 派生结果和实际 keyword 检索，document ID 为 `document_1391e82a53cf43c342a3852b1c832829f24fa3eff4ff1b1c50c6e8e4501f2479`。

另建 `sources-wheel-47e151d5d11b`，只安装同一 wheel 的 `[sources,images]` 及其依赖，没有开发依赖、数据库 SDK 或模型运行时。两个 `python -I` 进程均从仓库外工作目录执行：真实 HTTP 下载猫图→图片解码/持久化，生成的原生 DOCX→文字/表格/内嵌图解析，核对下载清理、codec 身份、Markdown 和持久 Asset SHA。两个进程都通过；图片 ID 为 `document_d57a07a22ce75a0f7d653a478a187810a76f4572190ded5c9b457546bf7b1f8c`，DOCX 为 `document_341a5a8f994775b382904e3b71e2a58dacc48a88f5ffb3f7b60f95536b1d6bb2`。该环境的 boto3/botocore 为 1.43.121，httpx 0.28.1，Pillow 13.0.0，lxml 6.1.3。

这些安装环境、调用脚本与可读回状态位于 `/Users/comet/.claude/jobs/d5cb2ec6/tmp`；它们是本地验收产物，不随 wheel 发布。核心可复验入口仍为 `scripts/check_core_install.py`；来源/图片调用脚本为该任务目录的 `verify_installed_sources_47e151d5d11b.py`，结果在 `installed-sources-external-47e151d5d11b/state.json`。

## 实际版本

Python 3.12.13；PostgreSQL 17.11；Milvus 2.6.23；etcd 3.5.16；SDK/工具：

| 包 | 当前验证版本 |
|---|---|
| torch | 2.14.1 |
| transformers | 4.57.6 |
| Pillow | 13.0.0 |
| httpx | 0.28.1 |
| SQLAlchemy | 2.1.6 |
| asyncpg | 0.32.0 |
| pymilvus | 2.6.17 |
| lxml（开发） | 6.1.0 |
| boto3 | 1.43.120 |
| pytest / Pyright / Ruff | 9.0.2 / 1.1.411 / 0.15.9 |

模型 revision、权重和图片 SHA 固定在 [examples](../examples/README.md)。本地文件 SHA 可以证明本轮实际使用的字节；不是远端部署永不变化的承诺。核心外部环境 lxml 6.1.3 与开发锁定版本不同，均实际检查。

## 功能提交

本轮审查修复按职责提交，未推送，commit 不含 AI 署名：

- `838ef01`：批量首次失败立即停止派发并排干已启动工作。
- `54fcc5d`：共享 URL 凭据检测，补齐持久快照和下载的验证边界。
- `ef61c96`：按官方 MinerU 4.x 单请求协议修订适配器、反例测试和严格集成入口。
- `7516c66`：清单 v2 保存实际向量并精确重放，PostgreSQL JSON 保留数值表示，使用新 schema。
- `ec0f74f`：Milvus 网络操作迁到原生异步 SDK，更新示例、取消与真实集成验证。
- `fe57d3a`：文本/代码按原始范围物化并升级 profile，消除全文重复和平方级存储增长。
- `c406dec`：完整记录 CLIP 分片权重和 tokenizer/processor 文件指纹。

此前阶段实现提交：

- `0ca651f`：受限来源获取、原生 TXT/DOCX 与 Markdown 派生导出。
- `b9d4c53`：范围切分、实际单元/表示、严格 codec 与嵌入模型。
- `ea00470`：PostgreSQL 内容事务/清单及 Milvus dense/BM25。
- `0872b6c`：持久图片资源与明确 partial 的 MinerU 协议；未将未验收服务标为通过。
- `ff74e18`：入库/搜索、精确删除、单元归并、父上下文与可选重排。
- `35a9f01`：可运行的持久文本/图片示例与真实模型策略评估。
- `925d906`：构建/CI、核心安装脚本、架构守卫和完整验收记录。
- `3049327`：公开 AWS S3 匿名实际下载、解析、清理和预算拒绝的真实集成测试。

此前阶段 D 的真实重启出口已关闭；本轮修订只重新执行入库与新进程读回，未再次重启。C 的公开 AWS S3 只读已通过，F 的独立包交付检查已通过；最终全阶段验收仍有以下边界。

## 剩余外部验收与限制

1. MinerU：已改为官方 4.x 自托管 `/v1/file_parse`，核对版本 4.0.12；仍缺少明确可用的实际部署，`COMET_RAG_TEST_MINERU_URL`、VERSION 和 TIER 未配置。官方异步 SDK 与 HTTP 选型依据见 [C](phase-c.md)。MockTransport 与官方源码只证明适配代码；公开云 health 的 API 1.0.0 不证明自托管解析器版本/路由。此前旧 2.6.8 包执行被自动审批拒绝，未执行或绕过。这仍阻止 C 与最终全阶段验收关闭。
2. S3 范围：实际支持结论限于本轮 AWS 匿名只读。兼容服务、认证与版本对象未验证；自动审批仍拒绝执行 `chrislusf/seaweedfs:3.85`，理由 `[Code from External]`，未启动或绕过。
3. 远端 GitHub Actions 未运行；本地等价检查通过不冒称远端 CI 成功。正常服务重启可读也不等于断电耐久。

此前旧测试容器的重启拒绝仍被遵守。代码 `925d906` 验收时另建独立项目，对该项目新容器的显式重启获工具批准并实际成功，证据只用于当时新 scope；本轮审查修订未再次重启。没有读取私有配置/凭据，没有通过其它 agent 执行被拒动作。严格集成入口和环境变量见 [tests](../tests/README.md)。未完成项不 skip 为绿、不按已完成阶段报告。
