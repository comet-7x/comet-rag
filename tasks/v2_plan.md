# Comet-RAG V2：实施记录

当前进度：前十四批实现与验证已完成；第十四批补齐入库线程取消与清理，
并修复实机验证暴露的 ARQ 测试夹具关停竞态。
第十三批补齐统一查询 deadline 与超时降级。
第十二批补齐真实 RunnerRegistry worker 验收。
第十一批隔离任务 runner 的应用所有权。
第十批统一 Filter 语义，真实 Milvus 70 项通过。
第七批 Milvus metadata 契约与第九批参数化用例均已实机补验；第十二批补齐
Redis/PostgreSQL worker 与存储验收，其余缺口见下表。
下文按批次保留历史记录，现行 API 以第四批和版本说明为准。

## 当前剩余项复核（第十四批）

| 历史待办 | 对照当前代码的结论 | 后续安排 |
|---|---|---|
| 配置运行时赋值校验、初始化失败清理 | 第二批已解决，第四批完成异步入口迁移 | 保留回归守卫 |
| Milvus 原生异步真实验证、单测超过 10 秒 | 第五批补真实 Milvus 与故障恢复，第五/六批处理等待和 DOCX 热点 | 本批继续检查完整 unit 耗时 |
| VectorStore 与直接调用方的嵌套 metadata 所有权 | 复核确认内存向量写入、两路检索、snapshot、父块和 draft 共享嵌套值 | 第七批已修复并补共享契约 |
| BaseReranker 直接库调用的有限分数守卫 | 原同步/异步 score 放行非有限值，rank 的 top_k 还能隐藏末位坏分数 | 第八批已修复；保留服务层校验和降级 |
| Filter 契约 | 第十批已统一现有两实现的类型、缺失/null、bool/数字、标量 IN 与安全数值范围，真实 Milvus 验收通过 | 参见 filter_semantics.md；新算子、完整 IR、额外后端/schema 仍单独决策 |
| 全局 runner 注册表 | 第十一批已复现多 Context 错用 runner，并改为显式 RunnerRegistry；执行、ARQ 道次路由与清理使用同一实例 | 第十二批已补真实 Redis/PostgreSQL 多 Context、跨道移交、故障补投和启动失败连接清理；保留回归守卫 |
| 入库线程取消与临时文件清理 | 复核发现 IngestRunner 正规化/分块仍用裸 to_thread，清理直接阻塞事件循环，与既有 run_sync 纪律不一致 | 第十四批已修复并完成故障注入、反向与全量验证；保留线程不可强杀的限制 |
| 其他真实向量后端 | Qdrant、Weaviate、Chroma、OceanBase 已调研，但没有适配器、装配或真实契约验收；当前仅 Milvus + InMemory | 现行 spec A9 不含多后端；接入第三个实现及扩展 Filter/能力模型需独立确认范围 |
| 统一查询 deadline | 第十三批建立每次 search 的绝对截止时刻，覆盖 KB 检查、模型排队、召回判活、重排和父块读取；保留取消排干 | 已补故障注入、HTTP 接线与 13 类反向验证；线程不能强杀、清理可超过预算，边界见 query_deadline.md |
| 真实后端与安装验收缺口 | 第十二批补真实 PostgreSQL/Redis 及 Milvus 全量集成；MinIO、外部模型/MinerU、core-only 重装仍未补验 | 第十四批三层覆盖率：整体 92.38%、tasks 95.84%；其余按相关改造另行验收 |
| M4 生产闭环 | Stager 仍止于 ready；生产激活/superseded、激活响应丢失恢复、GC 与完整接线未完成 | 继续按 m4_todo.md 失败矩阵独立推进 |

第十四批优先补齐已确认的入库线程所有权遗漏。M4 激活/GC/生产接线、其它向量库适配
与剩余安装/外部服务验收仍是独立工作；查询能力调研不等于后端已实现。

# 第一批：文档校准与配置合并

> 日期：2026-10-22
> 起点：`refactor/enterprise-reliability`，`11c099bb0e553ea304c121f00a6eb5d44f7713e4`
> 范围：现状与文档校准；`build_context` 的配置合并职责提取。
> 依据：[技能调研与启动方案](v2_refactoring_research.md)、用户确认的可读性优先原则。

## 本批目标与结果

- [x] 校准 M1～M3、M4 已有切片和生产闭环缺口。
- [x] 校准 CLI、依赖安装、目录职责、分层守卫和契约入口。
- [x] 将 PipelineConfig 合并从资源装配路径中提取为纯函数。
- [x] 验证显式覆盖、嵌套赋值、模型身份和输入不变。
- [x] 反向注入缺陷，确认新增回归测试能检测目标错误。
- [x] 完成独立代码审查、单元/内存端到端及静态检查。

第一批实现保持行为。用户允许后续为更清晰的架构作破坏性变更，但本次配置合并没有
需要改变公开接口或数据格式的证据。

| 边界 | 本批处理 |
|---|---|
| 库的公开导入、Pipeline、Hook | 保留 |
| `build_context` 签名和资源创建/关闭顺序 | 保留 |
| CLI、HTTP、配置字段和默认值 | 保留 |
| Task / VectorStore / DocumentStore 契约与持久化字段 | 保留 |
| `composition` 内部职责 | 新增 `pipeline_config.py` 处理配置合并；bootstrap 调用它 |
| Python 版本、依赖与工具链 | 保留 |

## 文档校准

`tasks/spec.md`、`tasks/architecture_plan.md`、`docs/architecture.md`、`docs/structure.md`
和本地 `AGENTS.md` 已对照实现与 CI 修正：

- M1～M3 已完成；M4 已有父子计算、DocumentStore、ready 续跑与 revision-aware 读切片。
- Store 已有激活和回收原语；生产入库的激活/superseded、故障恢复、GC 和完整接线仍待完成。
- `HierarchyBuilder` 在 engines；`IndexPlanner`、`IndexPlan`、`RevisionStager` 在 services。
- services 不导入 infrastructure；契约位于 ports 或通用 tasks 内核，真实适配器位于 infrastructure。
- 启动统一使用 `comet-rag`；依赖版本引用 `pyproject.toml`；契约表链接测试基类，避免另维护用例计数。

`AGENTS.md` 被现有 `.gitignore` 忽略，已更新的内容仅在本地生效。本批没有改变忽略规则；
需共享的架构规则和当前状态也已同步至上面的受版本控制文档。

## 配置合并的职责

`composition/pipeline_config.py::resolve_pipeline_config(limits, overrides)` 只计算有效配置。
六个 DOCX 限额保持显式映射，并发与窗口配置明确选择调用方值或部署值；没有引入通用
递归合并器，也没有改变资源创建顺序。合并仍在原来的 runner 注册位置调用。

保留的关键语义：

1. 调用方显式字段优先，即使该值与库默认值相同。
2. DOCX 的显式字段独立读取；仅修改嵌套对象也能生效。
3. `vision_model` 直接传递对象，避免 dataclass 实现被序列化为字典。
4. 调用方配置、嵌套配置及其显式字段集合均不被修改。
5. 既有顶层 `model_copy` 与 DOCX 重建校验边界保留。

现有 bootstrap 测试继续验证配置传到 runner，以及资源配对、闸门和关闭语义。
新增 `tests/unit/test_pipeline_config.py` 的 8 个用例直接覆盖合并边界；这些用例也在
从 HEAD 提取的原始合并片段上通过，确认它们描述的是既有行为。

## 验证

使用现有环境，以 `UV_CACHE_DIR=/tmp/comet-rag-v2-uv-cache uv run --no-sync --offline`
运行，未同步或安装依赖。

| 检查 | 结果 |
|---|---|
| 修改前 bootstrap + revision bootstrap + 分层测试 | 578 passed，1.69s |
| 修改后上述范围 + 新配置测试 | 588 passed，1.75s |
| 新测试对原合并实现的特征化验证 | 8 passed |
| 完整 unit | 两次均为 2290 passed、20 skipped、1 xfailed；10.01s / 9.55s |
| 内存 E2E | 49 passed，2.09s |
| Ruff | 全仓 check 通过，改动 Python 文件 format check 通过 |
| Pyright | 0 errors、0 warnings |
| 独立代码审查 | 无阻塞问题；修正了一处 docstring 歧义 |

反向验证分别注入七类缺陷：漏接 DOCX 部署限额、忽略显式默认值、漏掉嵌套赋值、
序列化模型对象、修改调用方输入、绕过 DOCX 校验、丢失非资源配置。每类都被目标测试
击中，随后恢复实现并运行完整验证。

单测新增计数包含 8 个配置用例和新模块触发的导入/分层参数化检查。20 个 skip 仍为
1 个显式启用的本地 DOCX 冒烟及 19 个非项目符号文档块；OMML 的已有 xfail 未变。

本批没有改引擎依赖、真实后端协议或持久化格式；未重新运行隔离 core-only 安装、
真实后端集成、性能评测或三层覆盖率合并。历史通过记录不作为本次验收结果。

## 第一批结束时的后续项

- 单测 10 秒预算在既有基线就有波动，本次为 10.01s / 9.55s；功能通过不能代表耗时预算稳定满足。
- 当前 Pydantic 顶层模型在构造后赋非法值，原有 `model_copy` 合并不会重新校验。
  后续如加强入口验证，应同时考虑在创建资源前失败的装配顺序，明确记录行为变化。
- 资源选择与初始化失败清理是下一批候选，先用调用关系和故障反例评估，不机械继续拆函数。
- M4 激活、GC 和生产接线仍按原失败矩阵独立推进，本批不代表 M4 完成。


# 第二批：配置校验与启动资源生命周期

> 日期：2026-10-22。基于第一批的未提交工作继续；不代表已发布 V2。

## 已确认问题与处理

| 反例 | 处理 |
|---|---|
| PipelineConfig 构造后可赋非法值，model_copy 不重新校验 | 从原始字段值重新校验顶层和 DOCX；保留子类约束与模型身份 |
| APPConfig 嵌套队列/超时可被赋成非法值；配置合并在创建资源之后 | 创建前重新校验 APPConfig、计算有效 PipelineConfig、检查所需后端配置 |
| 任一后续构造/闸门/runner 失败时，已有资源未回收 | 异步工厂登记每个自建资源；失败等待逆序回滚完成 |
| 四个适配器的第二客户端构造失败泄漏第一个 | 构造器自身回收半成品；正常关闭首个客户端出错也尝试另一个 |
| 重复 Context.aclose 再次关闭资源；全局 runner 保留已关闭引用 | 多次调用共享清理任务；按身份撤销注册；API/worker 清理已发布引用 |
| 关闭或回滚被取消，正在排干的资源可被中断 | 独立清理任务受 shield 保护，等待完成后传播调用方取消 |

不因缩短函数而引入资源容器框架。bootstrap 保留明确的选择、创建和登记顺序；
`resources.py` 只集中关闭方法兼容、错误隔离、注册撤销与取消等待。

## 迁移与所有权

- 组合根现为 `await abuild_context(...)`，同步 `build_context` 已移除。API、worker 和
  仓内直接调用已迁移；不使用新线程或另一事件循环桥接异步资源关闭。
- 失败只回收本次自建资源；成功后 Context 接管传入的资源。失败前的 gate 绑定会改变
  注入对象，不保证恢复旧 gate。适配器内部注入的 HTTP 客户端仍遵守其原所有权约定。
- 正常关闭顺序：执行器 → runner/Hook → 外围 Loader/Extractor → 向量/文档存储 →
  模型 → 自建数据库。初始化期间未发布 Context，回滚按实际创建顺序反向执行。
- 清理普通异常不会遮盖启动原异常；调用方主动取消在清理结束后传播。
- 配置字段、数据 schema、依赖、CLI/HTTP 接口和核心 Port 均未改变。非法的运行时配置
  现在会在启动时明确失败。保留密钥值，不通过 JSON 或脱敏输出重建配置。

## 验证

使用 `UV_CACHE_DIR=/tmp/comet-rag-v2-uv-cache uv run --no-sync --offline`，未安装依赖。

| 检查 | 结果 |
|---|---|
| 完整 unit | 2363 passed、20 skipped、1 xfailed，9.62s |
| 内存 E2E | 49 passed，1.75s |
| 全仓 Ruff / Pyright | 通过；Pyright 0 errors、0 warnings |
| 新配置测试 | 13 passed（本批新增 5 个） |
| 新装配生命周期测试 | 32 passed |
| 新 API/worker 生命周期测试 | 8 passed |
| 适配器故障注入与既有相关测试 | 157 passed（其中新增 25 个） |
| 独立审查 | 发现两处取消窗口并修复；复审无确定阻塞问题 |

本批新增 70 个行为用例；另有新资源工具模块触发的分层/导入参数化检查。
20 个 skip 和 1 个 xfail 与首批相同。

反向验证覆盖：配置绕过校验、子类/模型身份丢失、前置配置检查失效、漏启动回滚、漏撤销
注册、误关注入资源、重复关闭、runner 身份失效、关闭错误阻断清理、取消打断排干，以及
API/worker 状态清理和四个适配器的部分构造清理。配置 5 类、组合根 10 类、入口 4 类、
适配器 5 类变体均被目标测试检测。组合根与适配器使用独立临时源码副本，验证后恢复
基线再跑目标测试。没有删除失败测试，也没有依赖真实网络。

## 边界与后续

- 没有运行真实中间件集成测试、隔离 core-only 安装或三层覆盖率合并。
- 取消保护保证调用方等待清理，不保证强制终止进程或损坏的第三方 closer 能释放成功。
- runner 仍是进程级注册表；身份保护不等于支持同进程并行托管多个完整应用。
- OpenAI 等不在本组合根实际创建路径上的其他适配器未扩展审查；本批只改 Qwen embedding、
  Qwen reranker、MinerU 和 Milvus 的已确认缺口。
- M4 激活/GC/生产写入接线仍是独立待办；本批没有改变其生产启用条件。


# 第三批：检索失败边界与候选后处理

> 日期：2026-10-22。延续前两批未提交改动，不改生产 M4 启用条件。

## 确认的问题

1. 直接实现 RerankerPort 的适配器返回 None、浮点索引等值时，返回处理位于异常保护之外，
   整次查询抛错。NaN/Inf 会被标记为成功重排；实际 FastAPI 响应把分数输出为 null。
2. 重排器与召回候选共享 metadata，修改嵌套值后抛错会污染降级结果。
3. 父块回填在身份校验之前删除同 parent_id 的后续 child。父块缺失或错配时，两条命中
   只返回一条；较早的错误引用还能抢占较后的合法父块。
4. hybrid 的 gather(return_exceptions=True) 等两路完成才处理致命错误。判活失败、
   参数错误或单路取消发生后，另一通道如果阻塞，整次请求仍等待。重复取消还会再次
   中断通道正在运行的异步 finally。

## 改造范围与职责

- `services/reranking.py` 只验证重排返回的结构、数量、完整整数索引和有限分数。
  `_rerank` 负责调用与降级，传入独立 metadata 快照；验证和结果物化全部在保护内。
  有效结果以 replace 更新分数，始终从原候选取得正文和身份，保留各路诊断字段。
- `_parents` 负责稳定查询 ID 去重、单次批量 IO 和降级诊断；`_expand_parents` 纯函数
  逐项校验身份，仅合并成功回填的父块；`_parent_chunk` 物化父块元数据。所有失败 child
  保留完整排名，最后才截 top-k。同步澄清 `tasks/m4_t6_design.md` §7.3 的去重时点。
- `services/recall_execution.py` 管理固定两路任务的创建、取消和排干；原召回错误分类
  保留。可恢复错误转为通道结果，致命错误取消另一通道并等待清理，再传播原异常。
  重复取消不会再次中断通道清理，不向调用方暴露 ExceptionGroup。

没有拆散公共检索 DTO，也没有新增服务类、依赖或持久化字段。公共导入与 SearchQuery
签名保留；新增固定降级原因 `invalid_result` / `result_score_invalid`，数量和索引原因
保持不变。父块故障现在可能返回更多可用 child；致命通道错误不再等待兄弟请求正常完成。

## 验证

环境沿用 `UV_CACHE_DIR=/tmp/comet-rag-v2-uv-cache uv run --no-sync --offline`。
新增用例使用协议替身、内存适配器、ASGITransport 和 Event，不访问真实网络。

| 检查 | 结果 |
|---|---|
| 改动前检索/revision/API/gate 基线 | 105 passed，1.34s |
| 完整 unit | 两次均 2415 passed、20 skipped、1 xfailed；10.27s / 10.03s |
| 内存 E2E | 49 passed，1.81s |
| 全仓 Ruff / Pyright | 通过；Pyright 0 errors、0 warnings |
| 修改 Python 文件格式 / git diff --check | 通过 |
| 新行为用例 | 40 项：重排 22、父块 8、hybrid 生命周期 10 |
| 独立审查 | 三个边界均通过，未发现确定阻塞问题 |

新增模块还触发了分层/导入参数化检查；skip 与 xfail 和第二批相同。

反向验证在独立临时源码副本完成，14 类缺陷均被目标测试检测：

- 重排 6 类：返回校验逸出、bool 索引被接纳、NaN 被接纳、metadata 共享、吞掉取消、
  诊断字段丢失。恢复后新增 22 项通过。
- 父块 3 类：恢复校验前候选去重、去重早于身份校验、回填前截 top-k；分别触发 6、1、1
  项失败。恢复后相关 49 项通过。
- hybrid 5 类：致命错误等待兄弟、漏取消、未排干就返回、重复取消打断清理、可恢复错误
  错误终止；分别触发 6、2、6、1、2 项失败。恢复后相关 97 项通过。

**耗时预算仍未稳定满足。** 本批完整单测为 10.27s / 10.03s，均略超 10 秒；第二次
附带耗时分布，最慢 8 项均是既有 DOCX、并发和执行器测试。首批基线已有 10.01s / 9.55s
波动，不能据此排除新增测试的开销；本批不以功能通过代替耗时门验收，也未删除或缩弱
并发断言来压时间。

## 限制与后续

- 此次未运行真实中间件集成、core-only 重新安装或三层覆盖率合并。
- 通道清理沿用适配器自身的超时与取消协作；本批未引入统一查询 deadline，也无法强制
  结束忽略取消且不返回的第三方适配器。
- 本批隔离交给 reranker 的 metadata；尚未统一 VectorStore 与直接服务调用方之间的
  嵌套 metadata 所有权。内存查询结果的嵌套值仍有别名风险，需单独定义快照契约后处理。
- BaseReranker 的直接库调用尚未统一有限分数守卫；本批在服务读路径保证坏分数降级。
- M4 生产激活、响应丢失恢复、GC 和完整真实后端验收仍独立推进。


# 第四批：上游能力复核与全仓执行模式分离

> 日期：2026-10-22。用户明确要求本轮完成全部双模式类及调用方迁移，直接删除旧
> `a` 前缀接口；允许为职责和可读性作破坏性 API 变更。沿用前三批未提交工作。

## 重估原则与上游证据

本批纠正两类设计判断：不能从既有线程包装推断上游没有异步能力，也不能让一个
适配器实例同时管理两套执行模式和客户端。后续新增适配器必须先核查项目实际需要的
上游方法、初始化和关闭契约，再决定是否需要线程桥接；执行模式由类型表达。

[上游能力复核](v2_async_reassessment.md) 记录当前环境版本、源码位置和逐项判断，
覆盖全部直接运行时依赖和关键传递依赖，包括网络 SDK、数据库驱动、Web 框架、
解析/识别、配置、日志、迁移与标准库。版本快照不等于上游最新版本。

本地 PyMilvus 2.6.17 的 `AsyncMilvusClient` 已覆盖本项目的集合、索引、加载、读写和
关闭操作；据此删除同步管理客户端及 Milvus 网络 RPC 的 `to_thread`。核对依据为
本地安装源码及上游固定版本的
[AsyncMilvusClient](https://github.com/milvus-io/pymilvus/blob/v2.6.17/pymilvus/milvus_client/async_milvus_client.py)
和 [async gRPC handler](https://github.com/milvus-io/pymilvus/blob/v2.6.17/pymilvus/client/async_grpc_handler.py)。
Schema / IndexParams 构造属于本地计算，仍可同步调用。

## API 与职责调整

- 模型、来源 Loader、文档提取器、DOCX 清理器、Pipeline、HookRegistry 以及对应
  Port 按 `Foo` / `AsyncFoo` 分开；同步和异步类型使用同名方法。请求构造、结果
  转换、分块等纯计算仍共享，DocxConverter / DocxParser 不创建空洞的 Async 副本。
- 只需异步的向量存储明确为 `AsyncVectorStore` / `AsyncInMemoryVectorStore` /
  `AsyncMilvusStore`，不增加阻塞事件循环的同步包装。已有单一异步领域对象如
  DocumentStore、TaskStore、TaskExecutor 保留领域名称。
- 适配器每实例只持有本模式客户端，注入参数统一 `client=`；借用客户端仍归调用方。
  同步 `close()` 直接调用，异步 `close()` 必须等待。Context 保留独立的适配器托管
  契约；公共 Pipeline 只关闭自己创建的 Loader。
- 组合根现为 `await build_context(...)`，关停为 `await context.close()`。
  `abuild_context`、项目旧 `a*` 方法和导入别名均移除；SDK 自有 `aclose`、标准异步
  上下文协议和普通以字母 a 开头的方法保留。
- `PipelineHooks` / `AsyncPipelineHooks` 是相互隔离的默认注册表实例，类型为
  HookRegistry / AsyncHookRegistry。同步提取函数进入异步注册表必须显式使用
  `sync_extractor`，不再根据调用时机隐式切换。
- 全部仓内调用方、契约基类、测试替身、benchmark 代码和现行文档示例同步迁移。
  [版本说明](../docs/release_notes.md) 列出迁移表；没有兼容期转发入口。

共享 Gate 是有意保留的双模式基础原语：两个适配器类型必须争用同一份进程预算。
纯计算和值对象也不按执行模式复制。持久化字段、向量 schema 和 M4 生产启用条件
没有因本轮 API 拆分而改变。

## 确认的失败边界与修复

1. **线程取消后资源提前释放。** 文件写入、重命名、媒体准备、PDF 校验、DOCX 解析
   等同步工作不能被取消杀死。`engines.offload.run_sync` 等待线程结束后才传播取消，
   消费线程后续异常；重复取消也不会使调用方提前删除输入文件。网络请求继续等待
   上游原生异步客户端。
2. **批量失败后兄弟任务仍占用资源。** `engines.concurrency.map_concurrently` 使用
   固定 worker 数，保序返回；异常或取消只向 worker 发一次取消，再排干清理。
   异步 embedding、Pipeline 与 DOCX 视觉请求复用它。来源加载也补齐取消排干。
3. **协议同名掩盖执行模式错配。** 共享 DocxConfig 的 vision_model 可指向两种协议，
   仅检查成员名无法区分。清理器、提取器和组合根在调用前验证 describe 的执行形态，
   两方向错配均明确抛出 TypeError，避免泄漏 coroutine 或阻塞异步网络路径。
4. **拆分可能绕过并发与所有权约束。** 两种模型模板继续以 final 入口取 Gate，
   类型和契约测试同时覆盖两种模式、共享预算、借用客户端、构造/关闭失败和原生异步
   Milvus 管理操作。
5. **入库路由仍直接同步解析 DNS。** FastAPI 不会自动卸载异步函数内部的同步调用。
   `submit_ingest` 在 `run_sync` 中执行 SourcePolicy.check，来源策略仍只有一份同步
   实现；取消时等待准入检查结束且不创建任务、不提交执行器。重定向与模型媒体的
   相同策略回调已由各自异步适配器卸载。

Milvus 连接时机是明确的行为变化：原生 SDK 构造是惰性的，服务不可达可能到首次
被等待的 RPC 才暴露。`build_context` 成功及存活探针都不证明 Milvus 可用；没有为
恢复旧构造副作用额外增加权限更大的探测调用。

## 验证

继续使用现有环境和 `UV_CACHE_DIR=/tmp/comet-rag-v2-uv-cache uv run --no-sync --offline`，
未新增或安装依赖。单元故障注入使用替身、MockTransport 与同步事件，不访问真实网络。

| 检查 | 结果 |
|---|---|
| 完整 unit | 2688 passed、16 skipped、1 xfailed，10.29s |
| 内存 E2E | 49 passed，2.09s |
| 集成测试命令 | 6 passed、189 skipped，0.94s；真实中间件不可用，相关用例未执行 |
| 全仓 Ruff / Pyright | 通过；Pyright 0 errors、0 warnings |
| 修改 Python 文件格式 / git diff --check | 144 files already formatted；无空白错误 |
| 纯库冒烟 | 禁止导入服务中间件包的独立进程中，同步/异步 DOCX→分块结果一致 |
| API 扫描 | 运行时代码无公开 foo/afoo 方法对；项目旧调用与别名已迁移 |
| 文档专项 | 154 passed、15 skipped；检查符号、签名、方法、await 与可识别的执行模式 |
| 独立交叉审查 | 未发现新的阻塞问题；核对 Pipeline / Hook / Context 的模式与所有权边界 |

反向验证在临时源码副本中注入缺陷并确认失败，再恢复基线：

- Milvus 原生异步管理/关闭、Gate 两模式守卫和批量任务生命周期，共 22 类缺陷。
- 来源写入/重命名线程、批量排干、错误模式客户端和旧别名，共 12 个目标守卫被击中。
- 模型客户端模式、借用关闭、异步 Gate 旁路和旧别名；另将媒体准备退回裸
  `to_thread`，本地文件/URL 两种路径的 embedding/reranker 共 4 个取消用例失败。
- DOCX 线程未排干、删除视觉模式检查、视觉批量未排干分别触发 2、4、1 项失败。
- PDF 校验退回裸 `to_thread`，单次取消、重复取消及线程随后失败共 3 项失败。
- 文档漏 await、使用旧方法、同步模型注入异步 client 各触发 1 项失败。
- 入库准入恢复直接同步调用、退回裸 `to_thread`、跳过策略分别触发 1、3、1 项失败；
  新准入测试与既有 SourcePolicy 测试合计 44 项通过。

测试计数包含新增模块引起的导入/分层参数化项与文档块变化，不能把总数差额都视为
新增行为测试。已有 OMML xfail 保留；没有删除失败测试来获得通过结果。

## 验收限制

- 单测 10.29 秒仍略超 10 秒目标，耗时预算未稳定达标；未通过缩弱断言压缩时间。
- 实际 PostgreSQL / Redis / Milvus / MinIO、外部模型与 MinerU 联调未验收。集成测试
  的 skip 不作为真实后端通过；尤其 Milvus 原生异步行为仍需部署环境验证。
- 纯库冒烟验证可选依赖隔离，但没有新建环境重新执行 core-only 安装；CI 冒烟已补上
  AsyncPipeline / AsyncLoader 导入。没有运行 benchmark 或三层合并覆盖率。
- 线程排干无法强制停止卡住的同步扩展；取消完成时间仍受其自身耗时/超时约束。
- 本批完成全仓双模式 API 迁移，不代表 V2 全部业务目标或 M4 生产闭环完成。


# 第五批：真实 Milvus 验收与单测耗时

> 日期：2026-10-22。延续前四批未提交工作；不改变向量字段、持久化格式或 M4 启用范围。

## 真实环境与验收方法

本轮读取仓库 `.env` 的 Milvus URI，强制使用现有 `zhihao_test_database`；没有创建、
删除 database，也没有沿用应用数据库名。实际版本为 **PyMilvus 2.6.17 / Milvus
2.6.22**。普通 pytest 不会读取 `.env`，上一批默认 localhost 下的 skip 不能作为
远程服务不可用的证据。本机代理曾把 gRPC 转发失败；仅对本轮测试进程中的目标主机
设置 `no_grpc_proxy` / `no_proxy` 后，原生异步探活成功，未改全局代理配置。

真实测试开启 `COMET_TEST_REQUIRE_SERVICES=1`，连接失败不能静默 skip。每例使用
随机集合前缀，清理只针对自己创建的集合。新增生命周期夹具对清理失败直接报错。
原有 44 项契约/场景加 3 项原生异步生命周期测试完成实机验收；CI 的固定 2.6.23
服务 job 已加入新文件，不能把本次远程 2.6.22 通过等同于已运行该 CI job。
全部测试和反向注入结束后，只读检查确认集合数量回到运行前的 1 个。

## 确认问题与改动

1. **测试替身迁移遗漏被 skip 掩盖。** 全模式检索替身仍定义 `aembed_query`，且
   `cast Any` 绕过类型检查。改为继承 AsyncBaseEmbeddingModel，移除对应类型绕过。
   `tests/contracts/retrieval_scenario.py` 承载同一套三模式检索断言，内存 unit 与真实
   Milvus integration 分别调用；缺少中间件时也能检测替身接口过期。
2. **索引异常后的回滚可能误删共享数据。** SDK 调用报错不能证明服务端未提交。
   删除自动 drop，保留集合和原异常；只由显式 `ensure_collection` 负责恢复。
   取消后留下半套索引时，也不再等待一个可能已退出的创建者完成。
3. **恢复条件必须明确。** 将索引构造提为 `_create_indexes`；检查与修改分开。
   仅在必需字段类型、主键、auto_id、BM25 配置及维度全部兼容，且 sparse 索引确实
   不存在时，以相同参数补建。已有错误索引、详情未知和旧 schema 均拒绝修改；读写
   方法只校验。成功校验并加载后才缓存维度。
4. **局部校验不足以授权修改。** 交叉审查发现旧 BM25 校验漏掉 metadata 等必需
   字段。已补齐 id/text/metadata/dense/sparse 的存在与类型、id 主键和禁止自动 ID，
   无效字段在读取索引前就被拒绝。没有收紧未由 Port 承诺的 VARCHAR 长度。
5. **单测用固定延迟制造并发。** 模型与 Loader 测试每次请求 sleep 10–20ms，累计
   等待显著。改为 Barrier 明确凑齐在途请求后放行，使用 TaskGroup 在失败时取消并
   排干任务；屏障的 2 秒超时只用于报告故障。请求数量、峰值断言、无闸门对照、真实
   acquire_timeout 和同步线程竞争测试全部保留。

## 耗时证据

以现有环境离线运行，计时期间不并行执行其他测试。基线完整 unit 为 2688 passed、
16 skipped、1 xfailed，**10.40s**；收集约 1.18s，JUnit 各阶段累计 8.433s。

| 项目 | 改前 | 最终 unit 运行 |
|---|---:|---:|
| test_concurrency 累计用例耗时 | 1.231s | 0.669s |
| test_loader_gate 累计用例耗时 | 0.979s | 0.184s |
| 上述两文件合计 | 2.210s | 0.853s |
| 完整 unit | 10.40s | 8.79s |
| 默认 pytest（额外收集并排除其他测试层） | — | 9.03s |

两文件减少约 1.36 秒。AST 分层检查基线约 0.385 秒，不是此次主要原因，未增加缓存。
最慢 DOCX 单项约 0.41 秒；profile 表明热点是 python-docx 默认样式查找的重复工作，
不是样本构建。该生产解析优化需单独验证样式继承语义，本批未扩展修改。

## 故障注入与验证

新增 20 项单元行为用例：6 项索引恢复/边界、13 项必要字段/类型/主键/自动 ID 样本，
以及 1 项复用完整检索场景的模型契约检查；新增 3 项真实中间件用例。

- **实机响应丢失**：真正 await 上游建索引成功，另一个客户端加载并写入数据，再在
  客户端调用边界抛 TimeoutError；验证原异常传播、集合与数据保留、重试成功，以及
  关闭一个客户端后另一个仍可读取。这不是在网络层丢包的测试。
- **实机部分完成后取消**：真正建好 dense 索引，暂停下一步并取消；新实例在没有
  原实例内存标记的条件下补齐 sparse，写入及 BM25 检索成功。
- **实机幂等**：重复等待原生 create_index，确认服务端接受相同 dense/sparse 参数。
- 临时 pytest 插件恢复“索引错误就删集合”或禁用续建时，两个实机回归用例分别失败；
  插件只存在于临时进程，没有写入项目实现。
- 隔离源码副本反向验证覆盖误删、不续建、替换错误索引、修改旧 schema、跳过维度、
  提前缓存，以及遗漏 metadata/类型/主键/auto_id 和旧模型接口，均被目标测试检测。
- 并发测试反向验证：绕过异步闸门 9 项失败，强制 Loader 批量串行和 Gate 串行各
  1 项失败；恢复后全部通过，无未等待协程或悬挂任务警告。

| 最终检查 | 结果 |
|---|---|
| 完整 unit | 2708 passed、16 skipped、1 xfailed，8.79s |
| 默认 pytest | 同样 2708 passed，254 deselected，9.03s |
| 真实 Milvus 契约及生命周期 | 47 passed、0 skipped，154.56s；首轮同样 47 passed |
| 内存 E2E | 49 passed，1.96s |
| 全仓 Ruff / Pyright | 通过；0 errors、0 warnings |
| 本批 Python 格式 / git diff --check | 通过 |
| 独立交叉审查 | 字段完整性问题修复后复审通过，无新增阻塞发现 |

## 范围与限制

- 本轮仅验收真实 Milvus；没有重跑 PostgreSQL / Redis / MinIO / MinerU / GPU 服务
  的完整联调或三层合并覆盖率，M4 生产闭环仍是独立待办。
- 单测两种入口本机均低于 10 秒；不是跨硬件、带覆盖率或高负载下的绝对耗时保证。
- 补建失败仍传播原始错误并保留集合，不承诺自动后台重试；调用方须再次显式 ensure。
- CLI/HTTP/配置格式与 Port 方法签名没有改变。部署与复现方式已同步
  [部署说明](../docs/deployment.md) 和 [测试说明](../tests/README.md)。

# 第六批：DOCX 默认样式重复解析

> 日期：2026-10-22。仅调整 DOCX 格式解析的职责与重复工作，保持解析行为、快照、
> 公开 API 和依赖不变。

## 证据与改动

profile 与计数确认：python-docx 1.2.0 的默认样式查询会扫描整张样式表，原
`_get_run_fmt` 对每个 run 的四种属性分别读取字符/段落样式，最多触发 8 次扫描。
300 行表格用例共调用 `default_for` 2400 次；样本构建不在本次纯解析计时内。

新增 `engines/documents/docx/formatting.py`，由 `RunFormatResolver` 负责格式解析：

- 按样式 ID 和类型缓存 SDK 查询结果；默认、缺失、错误类型及多个默认样式的回退
  仍由 python-docx 决定，没有另写默认样式查找算法。
- 按样式 XML 元素和属性缓存继承结果，保留 `None` / `False` 区别；依然按
  “直接格式 → 字符继承 → 段落继承 → False”短路，避免读取被遮蔽的非法属性。
- `DocxParser.parse` 在原有锁内创建缓存，成功与失败均在 `finally` 释放；
  跨文档和修改同一文档后的重解析都会重新读取样式。
- parser 保留 XML 遍历、段落分类和 Markdown 输出；普通 run、真实 Hyperlink、
  字段结果与表格单元格统一调用格式解析器。

同步校准 DOCX 内部文档中的旧函数名、已删除的两遍段落提取示意与职责图。
`docDefaults` 不参与继承、下划线不生成 Markdown 标记、Hyperlink 样式整条跳过等
既有边界均由特征化测试固定，没有借性能改造扩展格式语义。

## 性能对照

同机 Python 3.12 / macOS，旧实现副本与最终实现预热后分别测量 7 次，取中位数；
文档构建、磁盘读写及解压不计入，计数/profile 另行运行。每次结果均与旧实现比较。

| 样本 | 改前 | 改后 | 默认样式扫描 |
|---|---:|---:|---:|
| 300 行、3 列网格的超跨度表格，每行一个有文本的单元格 | 403.35 ms | 16.79 ms | 2400 → 2 |
| 单段落 300 个无直接格式 run | 402.52 ms | 11.00 ms | 2404 → 6 |

这是两个固定样本的约 24× / 37× 提升，不是所有 DOCX 的整体倍数承诺。原先约
0.41 秒的截断告警单测，在最终完整 unit 的 JUnit 记录为 0.038 秒（含构建和加载）。
剩余段落分类/标题判断的扫描未纳入本轮优化。复现边界见 `docs/benchmark.md`。

## 回归与反向验证

新增 19 项定向用例：

- 14 项手写行为期望在优化前后都通过：四属性直接 False、字符/段落多级继承、
  Hyperlink id/name、字段代码链接、表格、默认/缺失/错类型样式、多默认样式、
  缺失 basedOn、docDefaults 既有边界、w:em、短路、跨文档/修改后/异常后重用。
- 2 项弱引用守卫验证成功与失败退出均释放本次格式解析器。
- 3 项不计时性能守卫比较 1 与 40 个 run/表格行的 SDK 默认扫描或继承访问次数。
  不依赖特定机器的绝对耗时。

隔离源码副本或临时 pytest 插件注入以下 11 类缺陷，均被目标断言检测到：
direct False 当未设置、继承 False 当未设置、None 缓存成 False、漏 Hyperlink id、
漏 Hyperlink name、缓存键遗漏类型、跨 parse 复用缓存、提前读取被遮蔽属性、
结束后保留解析器、禁用样式查询缓存、禁用继承结果缓存。工作区源码未被注入，
恢复后定向测试通过。全部 7 份快照的 SHA-256 与本轮开始前一致，未重新生成快照。

另取本地 4 份真实 DOCX，每份在标题编号开/关两种模式下分别重新加载后，用改前
副本与最终实现比较完整解析结果、blocks、text 与 metadata，8/8 一致。私有样本
及内容未写入版本库；它们用于本地差分验证，不替代可在 CI 重现的合成样本。

| 最终检查 | 结果 |
|---|---|
| 完整 unit | 2731 passed、15 skipped、1 xfailed，9.60s |
| 默认 pytest | 2731 passed、15 skipped、254 deselected、1 xfailed，9.40s |
| 内存 E2E | 49 passed，2.13s |
| 入库/检索 benchmark | 7 passed，3.75s |
| 全仓 Ruff / Pyright | 通过；0 errors、0 warnings |
| 本批 Python 格式 / git diff --check | 通过 |
| 独立实现审查 | 未发现行为回归；补齐字段结果与表格格式回归 |

完整 unit 与默认入口均在本机低于 10 秒，时间包含新增守卫；不承诺其他硬件、覆盖率
模式或系统高负载下的固定耗时。文档校准移除了一个过时 Python 示例，自动生成的
文档守卫因此减少三个通过项和一个跳过项；最终两种入口的 unit 收集集合一致。

## 验证范围

本轮不涉及真实中间件或模型适配器，没有重跑 PostgreSQL / Redis / Milvus / MinIO /
MinerU / GPU 联调、三层合并覆盖率或 core-only 重新安装。第五批真实 Milvus 验收
记录继续保留，不作为本轮新执行结果。V2 的其余业务目标与 M4 生产闭环仍独立待办。

# 第七批：存储 metadata 所有权与复制失败边界

> 日期：2026-10-22。先复核第三批遗留，再扩展到同一读路径的父块存储；
> 不改变 Port 签名、metadata 字段、持久化 schema 或依赖。

## 确认的问题与职责

原内存向量库只执行 `dict(metadata)`；写入后修改输入的嵌套列表、修改 search /
search_keywords 的命中或调试 snapshot，都会改变后续查询。两个记录复用嵌套输入时，
结果之间也会互相污染。embedding 已有 list 复制，本批仅以回归固定其正确行为。

DocumentStore 的 frozen dataclass 和 MappingProxyType 只保护外层。内存实现直接保留
RevisionDraft 和 StoredParent，前者的嵌套修改会改变重放冲突判断，后者会通过父块回填
暴露内部引用。新增公共服务测试在 HEAD 基线 15 项全部失败，确认污染贯穿 dense /
keyword / hybrid 和 parent 返回路径。

处理集中在各自边界：

- VectorStore 写入时独立复制各记录 metadata；两路查询排序截断后仅复制返回的命中，
  snapshot 也返回独立数据。整批校验与复制完成后再更新，复制失败不部分插入或覆盖。
- RevisionDraft / StoredParent 构造时复制嵌套 metadata，继续只冻结最外层，保留 dict/list
  形状。内存 DocumentStore 在 reserve / put / get 用 replace 复用构造器快照职责。
- reserve 在占用 revision_id 或增长 generation 前完成复制；put_parents 在任何发布前
  完成整个批次复制。失败后原记录保留，重试仍可完成。
- 服务层沿用存储返回的快照；未给每个候选处理步骤重复添加复制，也未机械改动 Milvus /
  PostgreSQL 的序列化代码。既有重排输入隔离保持不变。

上述保证针对现有 JSON 风格 metadata，以及调用完成后对输入/结果的修改；不承诺调用
进行中可并发修改输入，也不宣称任意自定义对象都可深复制或递归不可变。不可复制值的
异常继续传播；本批新增的是失败时不得留下部分存储状态的保护。

## 回归、故障注入与反向验证

新增 29 项行为用例：7 项后端共享契约、2 项值对象快照、5 项内存存储专项及 15 项服务
入口回归。向量的 5 项、DocumentStore 的 9 项分别在修改实现前全部变红；服务 15 项在
临时源码副本使用 HEAD 实现时全部变红。新增测试不访问真实网络。

复制异常注入覆盖：批次前段覆盖/新增后段失败、父块批次后段失败、首次或已有 revision
的 draft 复制失败。检查原值、记录数量、generation、占用状态及恢复重试，避免只断言
异常出现而放过副作用。

在独立临时源码副本完成 12 类反向验证，全部被目标断言检测：向量写入浅复制、dense
读取浅复制、keyword 读取浅复制、snapshot 浅复制、embedding 引用复用、向量逐条发布、
值对象浅复制、draft 引用复用、复制前消耗 generation、父块写入引用复用、父块读取引用
复用、父块逐条发布。恢复后 95 项相关测试通过；工作区未注入变异。

| 最终检查 | 结果 |
|---|---|
| 完整 unit | 2760 passed、15 skipped、1 xfailed，8.91s |
| 内存 E2E | 49 passed，1.82s |
| 入库/检索 benchmark | 7 passed，3.60s |
| 相关定向回归 | 95 passed |
| 全仓 Ruff / Pyright | 通过；0 errors、0 warnings |
| 本批 Python 格式 / git diff --check | 通过 |
| 独立实现与测试审查 | 未发现阻塞问题或已确认行为回归 |
| 新增真实后端共享契约 | 7 skipped；3 项 Milvus 连接/鉴权失败，4 项 PostgreSQL 本地未运行 |

沿用 `UV_CACHE_DIR=/tmp/comet-rag-v2-uv-cache uv run --no-sync --offline`，未安装依赖。
单测时间继续低于本机 10 秒目标；skip/xfail 与上一批一致。共享契约自动由真实后端测试类
继承，但本次跳过不算通过，也未重跑 core-only 安装和三层合并覆盖率。

下一批优先考虑 BaseReranker / AsyncBaseReranker 直接调用的非有限分数守卫：本次已用
Qwen 同步/异步适配器和 MockTransport 复现，服务层现有降级不能替代库边界验证。
全局 runner、多应用生命周期、统一查询 deadline 与 M4 生产闭环继续按独立契约推进。

# 第八批：重排基类的有限分数边界

> 日期：2026-10-22。处理第七批确认的直接模型调用缺口；保持同步/异步类分离，
> 保留服务层独立返回值校验和降级，不增加依赖或公开接口。

## 证据与改动

Qwen 的响应 DTO 将 `1e999` / `-1e999` 解码为正负无穷，将字符串 `"NaN"` 转为 NaN。
原 BaseReranker / AsyncBaseReranker 的 score 直接放行，rank 只校验数量再排序；
top_k=1 甚至能把末位 -Inf 隐藏在截断结果之外。因此校验不能只放在 Qwen DTO 或
rank 返回值上，也不能依赖服务层来保护直接调用模型的库用户。

本批生产改动集中在 `infrastructure/models/reranker/base.py`：

- 同步/异步 score 在闸门请求完成后调用同一个纯校验函数。rank 继续通过 score 获取
  分数，在排序与截断前检查全部项；没有额外抽象或一套异步校验算法。
- 拒绝 NaN、正负无穷、bool、非数值及无法转换为有限浮点数的超大整数，统一抛
  ValueError。错误只包含分数位置和固定说明，不把不可信值带入日志。
- 不归一化、不强制 `[0, 1]` 范围，正常负数、正负零、有限极值保持原值。供应商 DTO
  已支持的有限字符串转换继续由适配器负责，基类不再把原始字符串猜成数字。
- 不把数量校验下沉为 len(documents)：Qwen 低层还接受单个字符串/多模态 DTO，数量
  应由供应商解释；rank 的候选数量和排序规则继续保留。空结果保持既有行为。

服务生产代码没有修改。两层保护各有对象：基类抛出的 ValueError 进入已有
`request_failed` / `ValueError` 降级，仍返回原候选并记录日志；独立实现 Port 绕过基类
返回非法分数时，服务仍报告 `result_score_invalid`。这是 Qwen 非有限分数诊断的可见
变化，未增加诊断枚举。上游异常与取消保持原样传播，校验失败后闸门可继续使用。

## 回归、故障注入与反向验证

新增 59 项行为用例：基类 40 项、真实 Qwen 适配器配合 MockTransport 18 项、完整服务
降级与恢复 1 项。所有注入均零真实网络。基类初始样本在修改前为 30 failed / 6 passed，
Qwen 为 12 failed / 6 passed；随后补齐数字字符串拒绝用例并通过反向验证。正常用例
覆盖超出概率范围的有限值、负零、稳定排序、空批次、供应商 DTO 转换和单个文档输入。

服务回归在旧实现已能退回原候选，只在新诊断断言处失败；它验证两层保护的衔接，
不作为直接调用漏洞的独立证据。修复后，同一实例的下一次合法响应可以恢复重排。
已有独立 Port 的非法分数、HTTP 序列化及取消测试继续保留。

在独立临时源码副本完成 14 类反向验证，均被目标测试拦截：

- 绕过同步校验、绕过异步校验、放行 Inf、放行 NaN、放行 bool、把数字字符串强制转换、
  漏处理整数溢出、只检查首项、截断后才检查、误加概率区间限制、错误中回显供应商值。
- 删除服务分数守卫、服务重抛普通模型故障、服务吞掉取消。

恢复后相关 101 项通过。工作区源码未被注入，未删改既有服务守卫来使测试通过。

| 最终检查 | 结果 |
|---|---|
| 完整 unit | 2819 passed、15 skipped、1 xfailed，8.78s |
| 内存 E2E | 49 passed，1.89s |
| 入库/检索 benchmark | 7 passed，3.69s |
| 相关定向回归 | 101 passed |
| 全仓 Ruff / Pyright | 通过；0 errors、0 warnings |
| 本批 Python 格式 / git diff --check | 通过 |
| 独立审查 | 未发现阻塞问题；确认校验、供应商解释、服务降级的职责边界 |

使用 `UV_CACHE_DIR=/tmp/comet-rag-v2-uv-cache uv run --no-sync --offline`，未安装依赖。
完整 unit 包含上一批尚未提交的 metadata 改造，skip/xfail 与上一批一致。当前耗时在
本机低于 10 秒，不承诺其他硬件或带覆盖率运行的时间。

本轮未调用真实 GPU 模型或中间件，未重跑 core-only 安装与三层合并覆盖率；MockTransport
验证实际 Qwen 请求/解析路径与可重复的异常响应，不代表线上模型联调。第七批真实后端
契约验收、全局 runner/统一 deadline 与 M4 生产闭环仍按上方剩余清单推进。

# 第九批：多后端查询复核与 Milvus 值绑定

> 日期：2026-10-23。完整研究见 [查询与过滤适配复核](../docs/vector_filter_reassessment.md)。

## 复核与范围

重新检索五家官方仓库、文档/API、固定 PyMilvus 2.6.17 源码及社区问题。纠正 Chroma
常规查询后过滤、Milvus 无过滤 count/文本短语/地理时间能力、Chroma 无数组 metadata
等结论；补充 Qdrant 浮点 match 限制、Weaviate 分词与空值语义、版本/schema/索引门槛。
资料表和推论区分见报告，不宣称已实测五库或已核对五家全部 OpenAPI/客户端源码。

当前可见工作区没有原调研声称的 Filter AST 或 Milvus 参数化实现，只有前两批未提交
改动。因此本次基于现有字面量实现改造，保留已有工作，没有撤销或替换未知分支内容。

## 改动与可见行为

- 新增 Milvus compile_filter：生成模板和独立参数，dense、keyword、count 与删除前
  选取查询均接线；删除 ID 使用独立参数名，与 metadata 参数不碰撞。
- JSON 字段路径使用 JSON 字面量转义，覆盖引号、反斜杠及控制字符；参数化不能替代
  字段路径处理。旧 build_expression 仅保留导入兼容，生产 RPC 不再调用。
- 绑定值收紧到 str/bool/int64/有限 float 与同类型候选集合。None、异构集合、对象、
  NaN/Inf、溢出整数明确抛 ValueError；不字符串化、不自动回退拼接或去过滤重试。
  这是 Milvus 的可见输入限制，不代表已统一 InMemory 的缺失/null/bool-int 语义。
- Filter/AsyncVectorStore 的公共签名、向量 schema、依赖不变。研究提出的强类型 IR、
  schema profile 和新增后端仍需按 AGENTS/spec 的接口、schema 与实现数量边界决策。

## 故障注入与验证

新增 41 项单测及 1 项真实 Milvus 集成用例：编译结构、恶意值/字段键、同名参数隔离、
集合独立复制、SDK protobuf 序列化、原生 AsyncMilvusClient→传输层的参数实接，以及
四条调用路径的超时/取消传播、非法输入不进入查询或删除。集成用例验证 hostile value
下 count/dense/keyword/delete 的一致选取，当前环境连接/鉴权失败，实际跳过。

在独立临时源码副本注入 10 类缺陷，全部被测试检测：dense/keyword/count/delete
分别漏传绑定值、字段键不转义、标量全转字符串、占位符碰撞、放行非有限数、删除 ID
参数碰撞、超时后去过滤重试。恢复后 41 项通过，工作区未被注入变异。

| 最终检查 | 结果 |
|---|---|
| 完整 unit | 2860 passed、15 skipped、1 xfailed，8.66s |
| 内存 E2E | 49 passed，1.82s |
| 入库/检索 benchmark | 7 passed，3.25s |
| 定向 Milvus 编译/适配器回归（新增传输检查前） | 94 passed |
| 新增绑定与传输单测最终集 | 41 passed |
| 全仓 Ruff / Pyright | 通过；0 errors、0 warnings |
| 改动 Python 格式 / git diff --check | 通过 |
| 新增真实 Milvus 参数化用例 | 1 skipped，连接/鉴权失败，47 deselected |

使用现有环境 `UV_CACHE_DIR=/tmp/comet-rag-v2-uv-cache uv run --no-sync --offline`。
没有安装新依赖、重装 core-only 或合并三层覆盖率，也没有运行另外四库和 GPU 联调。
旧删除流程的先查后删并发窗口/大结果集、空集合服务器端行为与强类型过滤规范均未因
本次参数化自动解决。保留这些缺口，禁止将本地 SDK/替身通过写成真实后端验收通过。

# 第十批：Filter 契约与真实 Milvus 参数化验收

> 日期：2026-10-23。用户明确授权统一 Filter 类型和语义；未新增向量后端、schema、
> 依赖或持久化字段。现行规范见 [Filter 契约](../docs/filter_semantics.md)。

## 规则与职责

Filter 从 dict[str, Any] 收紧为 Mapping[str, FilterValue]，值为内建 str/bool/int/float
或同类型候选 list/tuple/set，字段间 AND。保留原有 Filter/matches_filter 导入位置。
ports/filtering.py 统一完整校验、候选快照和内存解释，Milvus compiler 不另维护类型规则。
HTTP 与 SearchQuery 在 Pydantic 转换前校验，存储再次校验，避免空库、空关键词、空 ID
或不命中的前置条件掩盖后续非法值。

- bool 与数字分离；范围内 int/float 按数值相等，字符串不转换。
- 查询值 None 拒绝；存储中的缺失/null/数组/对象均不能匹配标量谓词，空字符串仍有效。
- 集合表示标量 IN，不是数组成员或数组整体比较；异构候选拒绝，空集合匹配零条。
- 数值查询限于有限的 ±(2**53-1)。Milvus 实测浮点 2**53 会同时命中整数 2**53 和
  2**53+1，Python 不同；继续沿用上一批 int64 查询域会产生假命中，因此本批进一步
  收紧查询域而不重写已存 metadata。安全上下界另有真实边界测试。
- None/{} 读取无过滤；删除必须 IDs 或非空 Filter。首次共享矩阵确认原 memory 允许
  空过滤清库，而 Milvus 报空表达式无 limit；本批统一拒绝隐式清库，不构造恒真条件。
- Milvus 在首次 collection 发现 await 前完成参数快照，不因调用方后来修改候选改变
  已提交条件。超时/取消仍传播，不去掉 Filter 重试。

## 真实环境与故障证据

实际版本 PyMilvus 2.6.17 / Milvus 2.6.22。读取已有 .env URI，强制使用现有
zhihao_test_database，不创建或删除数据库。未加载 .env 或经本机代理失败都不作为
远端不可用的结论；测试进程仅针对该主机设置 no_grpc_proxy/no_proxy/NO_PROXY，
不改变全局代理。开启 COMET_TEST_REQUIRE_SERVICES=1，连接失败不能静默 skip。

先用随机集合探测 bool/数值、null/数组、空 IN 和精度边界；finally 清理。
首次完整运行暴露空过滤删除差异，为 61 passed / 1 failed；依据明确的删除安全规则
修复后最终完整重跑，而非删除失败测试。一次并发定位运行与完整测试交叠，使临时
检查器观察到其他测试的活动集合；最终串行运行已恢复完整起始集合基线。

新增 30 项专门单测、18 项两实现共享契约，以及 1 项真实数值/字面字段边界用例。
真值表预期独立手写，包含同批 bool/int/float/string/null/缺失/数组/对象/大整数记录；
每条验证 count、dense、keyword、delete，另检验非法条件不依赖是否命中、不得部分删除。
原有 hostile value 参数化与异步索引响应丢失/取消等生命周期用例一并实机验收。

隔离临时源码副本完成 14 类反向验证，均被目标测试检测：恢复 Python bool/int 相等、
禁止 int/float 等值、把存储数组当 any、接受查询 null、接受异构候选、漏数值安全范围、
四个内存操作分别漏完整校验、放行空过滤删除、HTTP 漏校验、服务 DTO 漏校验、参数
快照晚于首次 await。恢复后相关 118 项通过，工作区未注入缺陷。

| 最终检查 | 结果 |
|---|---|
| 真实 Milvus 向量/关键词契约与原生异步故障生命周期 | 70 passed、0 skipped，237.69s |
| 真实测试清理 | 最终集合列表等于运行前列表，数量恢复为 1 |
| 完整 unit | 2911 passed、15 skipped、1 xfailed，8.37s |
| 内存 E2E | 49 passed，1.91s |
| 入库/检索 benchmark | 7 passed，3.11s |
| 全仓 Ruff / Pyright | 通过；0 errors、0 warnings |
| 本批 Python 格式 / git diff --check | 通过 |

单元与本地验证使用 UV_CACHE_DIR=/tmp/comet-rag-v2-uv-cache uv run --no-sync --offline，
未安装依赖。单元新增通过数包括共享契约与新增基础模块触发的分层/导入守卫。
本轮不把真实 Milvus 验收等同于 PostgreSQL/Redis/MinIO/GPU、CI 固定 2.6.23、core-only
重新安装或三层覆盖率验收；删除并发窗口、全量大结果集与 ANN 召回限制仍保留。


# 第十一批：任务 runner 注册与应用生命周期隔离

> 日期：2026-10-23。基于前十批继续；注册表实现、调用方迁移、测试与文档按同一职责提交。

## 复核与选择

对照当前剩余表，复查了 Context 装配/关闭、任务执行与 ARQ 续跑路由的调用链。
原实现存在可执行反例：先装配 A，再装配 B，向 A 的执行器提交任务，实际调用 B 的
IngestRunner；关闭 B 后全局 ingest 绑定消失，A 无法继续执行。原先按身份解绑只防止
旧 Context 误删新 runner，不能提供隔离，失败启动也可能撤销其他存活应用的可用绑定。
因此本批先修复已确认的生命周期问题。查询统一 deadline 涉及各阶段预算与降级策略，
M4 仍涉及生产激活与回收，均保留独立待办。

## 改造与边界

- `tasks.runner.RunnerRegistry` 明确持有 kind → runner；删除进程全局注册表及其便捷函数。
  每个 StoreDrivenExecutor 默认创建独立注册表，也可通过 `runners=` 显式注入；绑定属性
  只读。InProcess 与 ARQ 的实际执行、ARQ 的续跑道次判断都从同一实例查找，无全局兜底。
- `Context.runners` 与自带执行器使用同一实例。`build_context` 支持显式 `runners=`；
  注入自定义 TaskExecutor 必须显式提供消费者使用的注册表，自带执行器与显式表不匹配
  时在创建资源前报错。重复装配到同一执行器拒绝覆盖。
- 启动失败只按身份撤销本次 ingest 绑定，保留调用方原有注册；成功后的 Context 接管
  注册表，先排干执行器，再撤销 Hook/runner 并清空表，最后关闭外围资源。显式替换或
  自定义 runner 也不会在关闭后继续持有已关闭资源。独立应用必须分别装配 I/O 资源。
- `register_ingest_runner` 显式接收表，默认拒绝重复绑定；测试替换 loader 时明确
  `replace=True`。所有仓内调用方、契约 fixture、崩溃恢复 worker 与分道 E2E 已迁移。
  原 worker E2E 只替换最后一个 worker 的全局 runner，现在逐个装配各自的 loader。
- TaskExecutor 的 submit/request_cancel/shutdown 抽象方法、task_id 队列载荷、Task 持久化
  字段、向量 schema、引擎依赖均未改变。全局注册 API 的破坏性迁移见
  [版本说明](../docs/release_notes.md)，生命周期说明见 [架构文档](../docs/architecture.md)。

## 故障注入与反向验证

新增 `tests/unit/test_runner_isolation.py` 18 项：两个 Context 的实际任务执行及两种关闭
顺序；第二个应用在注册后启动异常/取消；取消关闭时先排干再解绑；替换 runner 后清理；
两种执行器无共享/兜底绑定；ARQ 同 kind 的独立续跑路由；重复注册、注入配对与
自定义执行器消费表；失败启动保留既有绑定；ARQ 工厂转交显式表。除业务计算替身外，
任务认领、runner 查找和终态写入仍走实际实现，断言通过 TaskStore 观察结果。

在临时源码副本逐项反向注入缺陷，不修改工作区实现：共享默认表、忽略注入、执行查错
表、路由查错表、静默覆盖、忽略解绑身份、关闭保留替换引用、排干前解绑、回滚遗留
绑定、忽略配对、允许自定义执行器使用不连通的表、丢失 ARQ 注入转交、回滚清空调用方
原有绑定。每项都必须由目标测试断言失败，导入/收集错误不计作检测成功。

## 验证

使用现有环境 `UV_CACHE_DIR=/tmp/comet-rag-v2-uv-cache uv run --no-sync --offline`。
| 检查 | 结果 |
|---|---|
| 完整 unit | 2933 passed、15 skipped、1 xfailed，9.58s |
| 内存 E2E | 49 passed，3.40s |
| benchmark | 7 passed，4.37s |
| 完整 integration | 6 passed、219 skipped，1.22s |
| 新增隔离/故障测试 | 18 passed |
| 反向验证 | 13 类均由测试断言检测，无收集或导入错误 |
| 全仓 Ruff / Pyright | 通过；Pyright 0 errors、0 warnings |
| 改动 Python 格式 / git diff --check | 通过 |

unit 的既有 skip/xfail 未改变；本次未更新 DOCX 快照。最终检查前人工复核了所有
生产改动及调用方迁移，补齐继承 ArqExecutor 的故障替身的显式注册表注入，防止
中间件未启用时的 skip 掩盖调用迁移遗漏。

本机 Docker daemon socket 不存在，未启动真实中间件。完整 integration 已执行，
6 passed、219 skipped（1.22s）；这些跳过不代表真实 Redis/PostgreSQL/Milvus 链路验收。
本批没有重复第十批的远程 Milvus 验证，也没有重跑 core-only 安装或三层覆盖率合并。


## 提交前分批复核

单独提取本批累计源码，核对所有调用方迁移、18 项回归与迁移说明一并存在，且不依赖
后续真实 worker 辅助模块。提交候选完整 unit 2933 passed（9.02s），E2E 49 passed；
全仓 Ruff、改动 Python 格式、Pyright 和 Commitizen 均通过。提交消息明确标记
BREAKING CHANGE，说明全局注册函数移除及显式注册表迁移。

# 第十二批：真实 Redis/PostgreSQL worker 与失败清理验收

> 日期：2026-10-23。在第十一批 RunnerRegistry 改造之上，补齐真实中间件验收与夹具修复。

## 环境与范围

启动已安装的 Docker Desktop，创建专用 compose 项目 `comet-rag-v2-worker-validation`，
使用 PostgreSQL 17.11、Redis 7.4.11，仅映射 127.0.0.1:25432/26379；数据库为
`comet_v2_test`，应用现有三条 Alembic 迁移，不使用仓库开发数据卷。
Milvus 使用已授权的 `zhihao_test_database`，真实服务器 2.6.22；测试前后对比完整
collection 列表，随机测试集合必须清理，原有集合保持一致。

## 确认的问题与改动

- worker E2E、PostgreSQL API E2E、全栈 E2E 均曾写死 localhost:5432 和开发库名，
  worker E2E 还写死 localhost:6379。这会让探测/清表与实际应用连接到不同实例，
  也会使 CI 的独立数据库设置失效。测试连接配置集中到 `worker_config.py`，
  从真实 fixture 的 DSN 保留地址、端口与解码后的凭据，仅显式选择测试 Redis DB。
- 原 worker E2E 使用 `async_run()`，收尾只关闭 Context，遗漏 ARQ 自建 Redis 池。
  上游 `Worker.async_run` 明确不关闭连接；两项回归在原夹具上均失败。夹具改为
  停全部轮询、排干全部 worker job，再调用原生 `Worker.close()`，复用生产
  on_shutdown，统一撤销 Context/executor 引用并释放 SDK pool。
- 全量集成中的 Milvus 接线冒烟未指定测试 database，且 RPC 异常时不关闭客户端。
  现在显式传入 fixture 的测试库与超时，并在 finally 关闭；正常/故障均有零网络回归。
- 新增真实 worker 用例与现有跨道用例进入 task-integration CI，强制依赖服务存在，
  不让本地 skip 代替持续验收。生产 RunnerRegistry/Port/数据库 schema 无需新增改动。

## 真实故障与观测边界

`test_runner_registry_workers.py` 七项用例通过实际 build_context/build_settings：

1. 两套部署的六个 Context 使用独立注册表和数据库连接池，同 kind 任务走自己的
   CPU/IO runner；任意一套先关闭，另一套仍能继续执行。
2. CPU 完成移交并关闭自己的 Context 后，IO 从 PostgreSQL 上下文与 Redis 的 task_id
   续跑；IO 第一次失败的变体保留重试预算，CPU 阶段只执行一次。
3. 注入移交 enqueue 失败，停止消费者后从生产 maintenance 对账补投，明确断言消息
   进入 IO 队列，防止错投 CPU 后靠再次移交掩盖路由缺陷。
4. 注册后启动异常或取消：注入已打开真实连接的自建工厂资源，先证明 PostgreSQL
   application_name 与 Redis client_id 存在，再从独立观察者确认连接消失；失败 Context
   不发布、只撤销本次 ingest 注册，存活应用仍能执行任务。

首次编写移交断言时，只观察 PostgreSQL PENDING 就立即读取 Redis，真实运行出现
“状态已落库、消息尚未投递”的窗口。测试现等待两个事实成立；没有用 sleep 掩盖，
也没有把两套中间件误写成原子事务。持久化 PENDING 的补投恢复单独由故障用例验证。
向量与模型不参与此组任务；实际 RAG + Milvus 的完整 worker E2E 另行运行。

## 反向验证

临时源码副本检测十一类缺陷：PostgreSQL 地址错误、Redis 地址错误、Redis DB 错误、
Milvus 冒烟连错库、冒烟客户端泄漏、跨 Context runner 查找、恢复道次错误、启动失败
遗漏 PostgreSQL 回收、遗漏 Redis 回收、遗漏 runner 撤销，以及 worker SDK pool 未关闭。
每类均须由目标测试断言失败，不以收集/导入错误计为检测成功。工作区实现未注入缺陷。

## 最终验证

使用现有环境 `UV_CACHE_DIR=/tmp/comet-rag-v2-uv-cache uv run --no-sync --offline`，
覆盖率数据另存临时目录，不覆盖仓库已有数据；不重新安装依赖。

| 检查 | 结果 |
|---|---|
| 既有 Redis/PostgreSQL 执行器、存储、崩溃恢复、跨道基线 | 117 passed、0 skipped，17.12s |
| 新增真实 worker 场景 | 7 passed；最终包含在全量 integration 中 |
| 新增 unit 端点/客户端/SDK 关闭回归 | 7 passed |
| 最终完整 unit（无覆盖率插桩） | 2940 passed、15 skipped、1 xfailed，8.31s |
| unit 覆盖率运行 | 2940 passed，88.75%（≥ 70%）；插桩运行 11.01s，不作耗时预算依据 |
| 内存 E2E + 覆盖率追加 | 49 passed，2.88s |
| 修复 SDK 关闭后的完整 integration + 覆盖率追加 | 230 passed、2 skipped，295.96s |
| 三层整体 / tasks 覆盖率 | 92.35% / 96.29%（tasks 为 856/889；90% 门槛通过） |
| benchmark | 7 passed，3.68s |
| 反向验证 | 11 类缺陷全部被目标测试检测，无收集/导入错误 |
| 全仓 Ruff / Pyright | 通过；Pyright 0 errors、0 warnings |
| 改动 Python 格式、workflow YAML 接线、git diff --check | 通过 |

全量集成也在 SDK 夹具修复前运行过一次（230 passed、2 skipped，294.57s），最终
验收以上表修复后的重跑为准。只有未设置 MinerU URL、MinIO 未运行两项跳过；脚本
审计其他 skip 为错误，不把 PostgreSQL/Redis/Milvus 暂时失联当作成功。
Milvus 两次验收后都恢复原有的 1 个 collection 基线；本次创建的临时 Docker 项目、
网络与匿名卷在验收后清理。ARQ 0.28.0 的原生 Worker.close 内部调用 Redis.close，
产生已观察到的弃用提示（最终 24 条），没有用替代 SDK 实现来隐藏它。

本批已补真实 PostgreSQL/Redis 与三层覆盖率缺口；core-only 独立重装、MinIO、
外部模型及 MinerU 仍未补验。没有更新 DOCX 快照或改变生产数据 schema。

## 提交前分批复核

本批包含真实 worker 场景、7 项 unit 回归、端点/SDK 资源清理修复、CI 接线和验收说明。
累计候选完整 unit 2940 passed（8.70s），E2E 49 passed；全仓 Ruff、改动 Python 格式、
Pyright 和 Commitizen 均通过。真实集成、三层覆盖率与反向注入结果已核对原始报告，
沿用本批上面的实机记录。


# 第十三批：统一查询 deadline 与超时降级

> 日期：2026-10-23。基于 `c8796f8`；按执行机制、检索业务与部署接线拆分本地提交。

## 复核与确认的问题

上轮通道取消排干已正确，但只限制模型闸门排队时间；KB 读取、SDK 连接池等待、
模型请求、召回与判活、重排和父块读取没有统一工作预算。只给每个阶段配置独立
相对超时会累加总等待；在整个 search 外套超时则会丢弃 hybrid 已完成的安全结果，
并将本可回退的重排/父块超时变成查询失败。这是本批确认并处理的缺口。

## 实现与职责

- `services/query_deadline.py` 只负责单调时钟剩余预算、阶段任务所有权、一次取消和排干；
  不认识后端、RRF 或降级规则。采用标准库 asyncio.wait/shield，没有新增依赖。
- 每次 RetrievalService.search 创建新 deadline，默认 30 秒；配置为
  `retrieval.timeout_seconds`，直接使用服务时为同名构造参数。构造/配置拒绝非有限、
  非正数和 bool；部署配置仍在创建资源前重新校验。
- KB 身份检查计入预算；每条召回通道的预算覆盖 embedding 排队、后端调用及全部
  revision 判活/补召回。hybrid 两路共享绝对时刻，不按阶段重置，也不保留固定比例。
- 单路请求未完成安全召回则抛 QueryDeadlineExceeded，HTTP 504。hybrid 保留已完成
  的成功通道；两路均失败且至少一路耗尽预算时为 504，普通双路故障仍为 503。
- 重排和父块只使用剩余时间，超时分别退回原候选和已判活 child，记录
  deadline_exceeded；成功重排后的父块失败保留重排分数。预算已尽不启动后续 IO。
- 调用方取消继续传播，包括在超时排干期间重复取消；不转换成质量降级。迟到成功、
  吞取消后成功均被拒绝；召回清理暴露的致命异常仍传播。
- 模型闸门原有 Overloaded 纪律保留：先触发的闸门拒绝仍走原有 429/重排降级。
  供应商自身 TimeoutError 不冒充共享预算耗尽。
- 不改 Port、数据库 schema、向量 metadata 或引擎依赖。配置样例、部署/架构/版本说明
  与测试入口已更新；完整语义见 [查询 deadline](../docs/query_deadline.md)。

预算从服务入口开始，不覆盖之前的 HTTP 接入/请求解析及之后的响应发送。线程不能强杀，
取消后须等线程工作结束再释放闸门，因此实际返回可超过预算；CPU 同步计算也不能抢占。
本批没有把工作预算包装成硬性的 HTTP 耗时 SLA。

## 故障注入与反向验证

新增 28 项定向服务/生命周期用例，覆盖三种召回模式耗尽、hybrid 单路存活、重排/父块
分别耗尽、阶段累计预算、真实 Gate 等待者清除与恢复、先触发的 Overloaded、
active 判活阻塞、KB 阻塞、调用方取消、线程桥资源所有权、不同查询预算隔离、迟到成功、
吞取消及清理致命异常；新增配置/HTTP 用例与 1 项真实应用装配的无网络 E2E。

在临时完整副本中依次注入 13 类缺陷：每阶段重置预算、父块另起预算、接纳事件循环迟到
结果、接纳吞取消成功、漏排干、吞调用方取消、重排/父块超时不降级、丢弃 hybrid 成功路、
隐藏清理致命错误、放行无限配置、漏接配置、504 错映射。每类均有断言失败，全部退出 1，
无收集错误；工作树未注入缺陷。另以 RuntimeWarning 视为错误运行 60 项定向回归，确认
没有未等待协程等运行时警告。

## 全量验证

使用现有环境及 `uv run --no-sync --offline`，未同步或安装依赖。

| 检查 | 结果 |
|---|---|
| 完整 unit（未加覆盖率） | 2982 passed、15 skipped、1 xfailed；9.63s |
| unit 覆盖率 | 88.83%；新增 QueryDeadline 模块 100%；插桩运行 12.42s 不作耗时预算 |
| 全量内存 E2E | 50 passed；4.57s（带覆盖率） |
| benchmark | 7 passed；4.41s |
| 全量真实集成 | 230 passed、2 skipped；291.97s |
| Ruff / Pyright / diff check | 全仓 Ruff 通过；Pyright 0 errors / 0 warnings；diff check 通过 |
| format | 初次验证改动/新增 10 个 Python 文件通过；分批提交时执行原语测试独立成文件，11 个文件通过；全仓 20 个既有问题逐项在 HEAD 复现，未混入格式清理 |
| 三层合并覆盖率 | 整体 92.39%、tasks 96.18%，均达标 |

真实集成使用专用 PostgreSQL/Redis 测试容器及已约定 Milvus 测试库
zhihao_test_database；Milvus 服务端 2.6.22，测试前后 collection 集合完全一致（1 个）。
临时 PostgreSQL/Redis 容器、专用网络与数据已清理。仅未配置的 MinerU 与缺失的 MinIO
各跳过 1 项；其他集成 skip 会由本次运行的审计插件拒绝。24 条 warning 来自上游 ARQ
调用 Redis.close 的弃用提示。DOCX 快照未更新。

core-only 隔离重装、外部模型/MinerU 和 MinIO 仍未补验；M4 激活/GC/生产接线仍按
m4_todo.md 的失败矩阵推进。后续候选不再将“没有统一查询 deadline”列作未完成问题。


## 第十三批提交前核验

按职责拆为两个可独立运行的提交：

1. `feat(retrieval): add cancellation-safe deadline execution`：QueryDeadline 执行原语、
   独立的迟到结果/吞取消/清理异常测试，以及执行机制文档。此提交不改变 search 行为。
2. `feat(retrieval)!: enforce shared deadlines across query stages`：检索服务接入共享预算、
   各阶段降级、配置与组合根、HTTP 504、服务/配置/HTTP/E2E 回归和完整操作说明。
   破坏性变更说明明确默认查询预算从无统一限制变为 30 秒。

从原 HEAD 分别构建第一项及两项叠加的完整源码快照，独立运行以下检查：

| 快照 | 完整 unit | E2E | 静态检查 |
|---|---|---|---|
| 执行机制 | 2949 passed、15 skipped、1 xfailed；9.46s | 49 passed；3.27s | Ruff / Pyright / 改动格式通过 |
| 完整接线 | 2982 passed、15 skipped、1 xfailed；9.79s | 50 passed；2.95s | Ruff / Pyright / 改动格式通过 |

两个提交消息均经 Commitizen 校验。拆分只移动 3 个原语测试到
`test_query_deadline_execution.py`，逐个比较 AST 确认全部原有测试函数及断言不变；
原有生产代码逐文件校验保持一致。测试入口文档已同步，原语回归不依赖尚未提交的服务接线。
拆分后再次运行 13 类反向注入，全部被目标断言检出，无收集错误。

本次提交准备没有修改生产行为或中间件测试；前述真实集成、benchmark 与三层覆盖率
属于实现轮验收，本轮未重新启动中间件或重复这些检查。

# 第十四批：入库线程取消与资源清理

> 日期：2026-10-23。起点 `ab5088b`；按入库线程生命周期、worker 夹具关停分为两项本地提交。

## 剩余项复核与选择

先前配置、重排、Filter、注册表和查询 deadline 的实现与回归入口仍在。
本轮发现执行模式重估还有一处实现与契约不一致：AsyncPipeline 已采用 run_sync，
IngestRunner 的正规化和分块仍裸 await asyncio.to_thread，资源清理仍在事件循环同步执行。
这会让已取消的阶段先退出，同步线程继续运行，甚至提前删除其输入；文件清理慢时还会
阻塞事件循环。因此先修补这条现有生产入库路径的线程所有权边界。

其他向量库的状态也已核对：Qdrant、Weaviate、Chroma、OceanBase 没有适配器、
配置装配或真实后端契约验收。已有五库调研、统一 Filter 类型与现有两实现的语义，
不等于已实现多后端。spec A9 仍限定 Milvus + InMemory，新增后端/IR/能力模型另行决策。

M4 激活、响应丢失恢复、GC 和完整生产接线仍按 m4_todo.md 推进；本批不更改
Task 持久化字段、Port、向量 schema、metadata、安装依赖或生产 revision 开关。

## 生产代码改动

只调整 IngestRunner 的三个边界，复用 engines.offload.run_sync：

1. 正规化取消时等待同步线程结束，再允许资源清理；取消后的成功和异常不继续写 context。
2. 分块同样先排干再传播取消，不发布迟到的 chunk_drafts。
3. LoadedResource.cleanup 移到工作线程，重复取消期间仍等待清理，不阻塞事件循环。

默认正规化/分块产物、异常分类、窗口化索引与正常数据格式保持原有行为。
HTTP/S3 Loader 的临时文件登记使用 threading.Lock，释放回调可在线程边界执行；
AsyncPipeline 也早已通过同一线程桥调用 cleanup。没有新增生产清理抽象。

同步工作不能强杀，因此请求取消或 Context.close 可能等待更久；文档不再把执行器的
关停 timeout 描述成强制终止线程的硬时限。跨进程租约回收与单进程部署的边界保留。

## 故障注入与补充发现

新增 11 项入库生命周期用例：

- 正规化/分块各覆盖一次取消、重复取消、取消后的线程异常；输入先保留到工作结束，
  晚到成功不写入 Task context，线程异常被消费。
- 同步文件清理阻塞时事件循环仍能推进，重复取消不提前退出。
- 实际 build_context + InProcessExecutor 在两种阶段、直接关停/先取消后关停四种组合下，
  仅通过 TaskStore 检查 CANCELLING/终态；不 gather 执行器内部任务。
  同步工作未结束时 Context 保持打开，线程结束后才关闭 Loader/模型、清空注册。

修复前最先新增的 7 项测试均在目标断言处失败，修复后全部通过。

首次完整真实集成得到 230 passed、2 skipped，但在
test_failed_worker_startup_releases_real_connections_and_preserves_live_app[True]
的 teardown 发生 CancelledError。检查已安装 ARQ 0.28.0 源码确认：

- worker.tasks 是 run_job 包装任务，包含 Redis 结果确认；job_tasks 才是函数本体。
- 原夹具把正在确认结果的 run_job 一起取消；Worker.close 再次 gather 已取消包装任务，
  在关闭 SDK 池之前退出。这是测试生命周期竞态，不能用“业务断言通过”掩盖。

用实际 Worker.close + 模拟网络池的两项确定性单测复现旧夹具错误后，抽出仅供
非 burst 集成测试使用的 worker_lifecycle.stop_workers：先停轮询、取消业务 job_tasks、
排干传输确认并解除已消费的 Task 引用，再由原生 close 执行 on_shutdown 与关闭连接池。
真实传输异常会在清理后继续传播；没有复制 SDK 连接关闭逻辑或修改生产 worker。

E2E 夹具使用 AsyncExitStack，排干异常也会继续尝试 SDK close。新增共 5 项 SDK 级
无网络单测，覆盖两条夹具入口、成功/失败的确认，以及业务任务取消和包装任务不被取消；
原有两项多 worker 顺序/异常清理测试继续保留。7 项真实 registry worker 回归通过。

## 反向验证

在隔离源码副本注入 11 类缺陷，全部产生断言失败（退出 1，无收集错误）：

- 入库 6 类：正规化/分块恢复裸 to_thread、清理放回事件循环、清理改用裸 to_thread、
  正规化前删除输入、吞掉线程桥取消。
- worker 夹具 5 类：取消传输而非业务、遗漏传输排干、吞确认异常、遗漏原生 SDK close、
  不取消业务任务。

另将 RuntimeWarning 作为错误运行 83 项入库、Context 和 SDK 夹具定向回归，全部通过。
正常流程和错误处理的期望来自输入文件、手写状态/顺序断言与实际 SDK close，
不通过重新调用实现计算预期结果。DOCX 快照未更新。

## 验证记录

使用现有环境与 uv run --no-sync --offline，未安装或更新依赖。

| 检查 | 结果 |
|---|---|
| 最终完整 unit | 2998 passed、15 skipped、1 xfailed；9.68s |
| 最终 unit 覆盖率 | 88.85%；IngestRunner 模块 97.35%；覆盖率插桩运行 13.37s 不作预算 |
| 内存 E2E | 50 passed；4.72s（带覆盖率） |
| benchmark | 7 passed；4.66s |
| 首轮完整真实集成 | 230 passed、2 skipped、1 teardown error；293.93s，已定位并修复上述夹具 |
| 定向真实 registry worker | 7 passed；3.82s |
| 最终完整真实集成 | 230 passed、2 skipped，无错误；292.66s |
| 三层合并覆盖率 | 整体 92.38%、tasks 95.84%，均达标 |
| Ruff / Pyright | 全仓通过；0 errors / 0 warnings |
| 格式与 diff check | 7 个修改/新增 Python 文件通过，diff check 通过 |

真实环境使用隔离 PostgreSQL/Redis 容器，以及 zhihao_test_database 中的随机 Milvus
collection；两轮结束时 Milvus 基线集合均完全恢复（1 个），最终测试仅跳过未配置的
MinerU 和未运行的 MinIO 各 1 项。其余 skip 会由本次运行的审计插件拒绝。
最终 24 条 warning 为 ARQ 原生 close 调用 Redis.close 的上游弃用提示；保留 SDK
原生关闭路径。专用 PostgreSQL/Redis 容器、网络和临时数据已清理。
MinIO、外部模型/MinerU、core-only 隔离重装仍未补验，不引用历史结果代替本轮验收。


## 第十四批提交前核验

按职责拆为两个提交，每项包含对应代码、测试与文档：

1. `fix(ingest): drain synchronous stages before releasing resources`：
   三处生产线程边界、11 个入库回归，以及架构/部署/版本说明、执行模式复核和测试入口。
   本项独立文档明确 ARQ 夹具修复仍在后续提交，不宣称完整真实验收已经无错误。
2. `test(workers): drain transport acknowledgements before closing pools`：
   测试专用 stop_workers、两处夹具接线、5 个原生 SDK close 回归、旧多 worker 测试替身
   以及最终验收记录；不包含生产业务代码修改。

分别从起点构建完整快照，保持原有生产代码与测试文件字节不变；共享文档按每项实际
内容分批更新。Ruff、Pyright、改动文件 format 与两条 Commitizen 消息检查均通过。

| 快照 | 完整 unit | E2E |
|---|---|---|
| 入库线程生命周期 | 2993 passed、15 skipped、1 xfailed；10.50s，独立耗时复核 10.14s | 50 passed；3.05s |
| 含 worker 夹具修复 | 2998 passed、15 skipped、1 xfailed；9.98s | 50 passed；2.86s |
| 当前工作区默认命令 | 2998 passed、15 skipped、1 xfailed；9.65s | 同完整快照 |

隔离快照使用 PYTHONDONTWRITEBYTECODE=1。首项两次超过 10 秒预算，实际工作区默认运行
在预算内，已分别保留数据；不据此推断环境差异的具体成因。耗时明细最慢 12 项均是
既有测试，新增入库回归不在其中。单测耗时存在波动，不能把快照功能通过等同于预算
始终满足；未修改测试断言、时间阈值或 pytest 筛选。

提交准备只重新运行本地检查。前述 230 passed / 2 skipped 真实集成、benchmark 和
三层覆盖率是实现轮验收，经记录核对仍适用于本轮未变的实现与测试；本轮未重启中间件。
