# Spec: M3 BM25 + RRF 混合检索

> 状态：已冻结（v1.0）
> GitHub Issue：[#54](https://github.com/comet-7x/comet-rag/issues/54)
> 开发分支：`feature/hybrid-search`
> 最后更新：2026-09-11

## 1. 目标

在不破坏现有 dense 检索的前提下，为同一知识库增加 BM25 关键词召回，并通过
纯计算 RRF 融合两路候选。精确术语、编号和专有名词由 BM25 补足，语义改写继续
由 dense ANN 覆盖；融合后仍可进入现有 reranker，失败时按通道降级而非整条读路径失败。

## 2. 非目标

- 不引入 Elasticsearch、OpenSearch 或新的中间件。
- 不实现第二个生产向量库后端，不把 Milvus 查询语法暴露给 Service 或 API。
- 不在首版实现 SPLADE、BGE-M3 sparse embedding、学习排序或加权融合调参平台。
- 不支持每个知识库选择不同 analyzer；首版面向中英文技术文档使用统一配置。
- 不自动删除、覆盖或静默迁移旧 collection，不承诺旧 collection 原地升级。
- 不借 M3 改 TaskStore、TaskExecutor、文档提取或分块契约。

## 3. 已确定设计

### D1 — 使用 Milvus 原生 BM25，但隔离在 KeywordSearchPort 后

生产实现复用现有 Milvus，不增加独立关键词服务。Milvus 负责 analyzer、BM25 sparse
生成和倒排索引；`services/` 只依赖 `KeywordSearchPort`，不得 import `pymilvus`、
`FunctionType.BM25`、`AnnSearchRequest` 或供应商 ranker。

```python
class KeywordSearchPort(Protocol):
    async def asearch_keywords(
        self,
        kb_id: str,
        query: str,
        *,
        top_k: int,
        filter: Filter | None = None,
    ) -> list[SearchHit]: ...
```

Port 有明确调用者 `RetrievalService`、真实实现 `MilvusStore` 和测试实现
`InMemoryVectorStore`，必须运行同一套契约测试。

### D2 — 固定 Milvus 2.6 稳定版本线

当前仓库 Compose 是 Milvus 2.5.4，而无上界的 `pymilvus>=2.5.0` 已解析到 3.0.1；
官方兼容矩阵不推荐跨主版本组合。M3 将服务端固定为 Milvus 2.6.23，Python SDK
固定为 `pymilvus>=2.6.17,<2.7`，不在首版升级刚发布的 Milvus 3.0。

版本更新与真实连接验证属于 M3-T3。在 Docker 未运行时只能证明客户端 schema API
可构造，不能把它记作服务端兼容验收。

### D3 — 新 collection 使用 BM25 schema v2

新建 collection 必须同时具备：

1. `text` 字段设置 `enable_analyzer=True`，首版 analyzer 为 `{"type": "chinese"}`；
2. `text -> sparse_vector` 的 `FunctionType.BM25`；
3. sparse index 使用 `SPARSE_INVERTED_INDEX` 与 `metric_type="BM25"`；
4. 写入时只提供原始 `text` 和 dense vector，不再手写空 sparse dict。

现有 schema 只有 sparse 字段，缺少 analyzer 与 BM25 function，且 sparse metric 为 IP，
因此并不兼容原生 BM25。`aensure_collection()` 遇到旧 schema 必须抛出可行动的
`CollectionSchemaMismatch`，提示删除并重新入库；绝不能自动 drop 或把 dense-only
collection 当作 hybrid-ready。

### D4 — 检索契约下沉，兼容旧导入

后端无关的 `Filter`、`SearchHit`、`VectorRecord`、错误和 `BaseVectorStore` 从
`infrastructure.vectorstore.base` 下沉到 `ports/`。旧路径保留同一运行时对象的兼容
导出；本阶段不改变 `BaseVectorStore` 已有方法签名，也不增加第三个实现。

`RetrievalService` 先在 M3-T2 使用窄的 `VectorSearchPort`；`KeywordSearchPort` 在
M3-T4 与两个实现及契约测试同时引入，避免出现没有调用者或实现的空抽象。入库、删除
和计数仍使用完整 `BaseVectorStore`。这样 Service 不再依赖 infrastructure，且读路径
不会获得不需要的写入能力。

### D5 — RRF 是纯计算 Strategy

`engines/retrieval/fusion.py` 实现 Reciprocal Rank Fusion：

```text
score(document) = Σ 1 / (rrf_k + rank_in_channel)
```

- rank 从 1 开始；默认 `rrf_k=60`；
- 按 chunk id 去重，同一 chunk 的通道贡献相加；
- 不归一化、相加或比较 dense/BM25 原始分数；
- 融合分数相同时按 chunk id 排序，保证内存与 Milvus 路径结果稳定；
- Strategy 只接收已排序候选，不 import Milvus，也不负责发请求或降级。

### D6 — 显式模式，默认行为保持兼容

`SearchQuery` 增加 `mode=dense|keyword|hybrid`，默认仍为 `dense`，避免升级后现有用户
在未重建 collection 时行为突变。`fetch_k` 作用于每个启用的召回通道；hybrid 先融合
到候选集，再进入现有 reranker，最终裁剪到 `top_k`。

响应增加实际模式、参与通道及 `keyword_score` / `fusion_score` 等可选诊断字段；旧字段
保留。`score` 始终表示当前最终排序分数，rerank 后为 reranker 分数，否则为对应召回
或融合分数。

### D7 — 通道级降级

- hybrid 中 dense 失败时记录 warning 并返回 keyword 结果；
- hybrid 中 keyword 失败时记录 warning 并返回 dense 结果；
- 两路都失败时抛出检索错误；
- 显式 dense/keyword 模式不静默切换到另一模式；
- reranker 失败继续沿用现有规则，返回融合或单路结果；
- 结果必须暴露实际参与通道和降级原因，不能只写日志。

知识库不存在、schema 不兼容和请求参数错误不是可降级的通道抖动，必须直接失败。

### D8 — 过滤与资源边界

两路召回共享现有结构化 `Filter` 语义：等值、集合包含、多键 AND。供应商表达式只能
在 Milvus adapter 内构造。每路 `fetch_k <= 500`，融合输入有界；M3 不增加新的模型
并发闸门，因为 BM25 是同一 Milvus 后端上的检索请求，不调用外部推理服务。

### D9 — 真实 Milvus 验证限定测试数据库

本开发环境允许复用 `.env` 中的 Milvus URI，但所有真实连接必须显式传入数据库
`zhihao_test_database`。不得读取或沿用 `.env` 当前的 `MILVUS_DB`，不得依赖 SDK 的
默认 database，也不得在尚未完成 `db_name` 配置和组合根装配前运行真实 Milvus 测试。

集成测试的 collection 使用测试专属前缀与随机后缀，并且只清理本次创建的 collection；
若目标 database 不是 `zhihao_test_database`，测试必须在建立连接前拒绝执行。模型配置
也可从 `.env` 读取，但密钥不得进入日志、测试快照、提交或失败信息。

## 4. 成功标准

### S1 — 分层与兼容

- [x] `services/` 不再 import `infrastructure.vectorstore`，AST 分层守卫覆盖该规则。
- [x] 旧 vectorstore import 路径仍可用，且指向同一契约对象。
- [x] core-only 路径可导入检索 Port 与 RRF，不加载 `pymilvus`。
- [x] `SearchQuery` 当前默认 dense，既有检索测试和 API 响应字段未回退。

### S2 — BM25 与 schema

- [x] Compose 与 PyMilvus 使用匹配的 2.6 版本线。
- [x] 新 collection 包含 analyzer、BM25 function 和 BM25 sparse index。
- [x] 旧 schema 被明确拒绝，测试证明不会自动删除已有 collection。
- [x] 中文术语、英文标识符和 metadata filter 的真实 Milvus 查询均命中。

### S3 — Port 与融合

- [x] InMemory 与 Milvus 通过同一 KeywordSearchPort 契约。
- [x] RRF 覆盖去重、单路缺失、并列排序、输入不变性与非法参数。
- [x] 反向注入过滤错误实现，确认 KeywordSearchPort 契约确实会失败。
- [x] 反向注入零基 rank 的错误 RRF 实现，确认性质测试确实会失败。
- [x] dense、keyword、hybrid 三种模式的候选数和分数语义稳定。

### S4 — 降级与装配

- [x] 单路失败只降级该路，两路失败才使 hybrid 查询失败。
- [x] reranker 失败返回融合结果，并记录日志与响应诊断信息。
- [x] 配置、组合根、API schema 和关闭顺序完成，路由不自行 new 资源。

### S5 — 真实链路与质量

- [ ] E2E 覆盖“精确术语靠 BM25、语义改写靠 dense、hybrid 合并两者”。
- [ ] 集成环境不可用时 skip，不 fail；真实 Milvus 可用时验证 analyzer 与混合链路。
- [x] 真实 Milvus 验证只访问 `zhihao_test_database`，且不会清理非本次创建的数据。
- [ ] 记录 dense/keyword/hybrid 的命中、延迟和候选规模，不用单个样本宣称质量提升。
- [ ] 默认 `uv run pytest` 仍小于 10 秒，Ruff、Pyright、core-only、integration、e2e 全绿。

## 5. 实施顺序

1. 检索值对象与 `VectorSearchPort` 下沉，建立兼容导出和契约测试。
2. 对齐 Milvus/PyMilvus 版本，实现并验证 BM25 schema v2。
3. 同时引入 `KeywordSearchPort` 及 InMemory/Milvus keyword search。
4. 实现纯 RRF Strategy。
5. 改造 RetrievalService、API、配置与组合根。
6. 完成降级、真实集成、E2E、基准和文档验收。

每项至少一个 Conventional Commit。collection schema 变更不得与 Port 迁移混在同一提交。

## 6. M3-T1 验证记录

- 基线：`1786 passed, 19 skipped, 177 deselected, 1 xfailed`，pytest 9.38s。
- Ruff、Pyright 通过，Pyright 为 `0 errors`。
- 本地 PyMilvus 3.0.1 可离线构造 analyzer、BM25 Function 与 BM25 index 参数。
- Docker daemon 未运行，未执行真实 Milvus 2.5.4 验证；版本对齐后的真实验证是 T3 硬门槛。

## 7. M3-T2 验证记录

- `BaseVectorStore`、检索值对象及错误已下沉 `ports/vector_store.py`，旧路径保持对象身份。
- `RetrievalService` 仅依赖 `VectorSearchPort`；AST 守卫用绝对、相对违规样本完成反向验证。
- 独立进程导入 Port 未加载 `pymilvus`；现有向量库契约全部通过。
- 全量结果：`1799 passed, 19 skipped, 177 deselected, 1 xfailed`，pytest 9.03s；
  Ruff 与 Pyright 通过，Pyright 为 `0 errors`。
- 未连接 Milvus；M3-T3 必须先显式装配 database name，再在 `zhihao_test_database` 验证。

## 8. M3-T3 验证记录

- Compose 固定 Milvus 2.6.23；锁文件与本地环境均为 PyMilvus 2.6.17。
- `.env` 目标的真实服务为 Milvus 2.6.22；所有连接显式使用
  `zhihao_test_database`，未读取 `.env` 中不同值的 `MILVUS_DB`。
- 27 条 Milvus 向量存储契约全部通过（171.53s）；中文“量子纠缠”和英文
  `TraitObject` 的原生 BM25 查询均命中，独立验证 12.73s。
- 所有测试使用随机 `cttest_*` 前缀；验收后残留测试 collection 为 0，数据库原有
  collection 数量仍为 1。首次失败遗留的两个临时 collection 已按完整名称清理。
- 远端仅有 1 个 streaming node，因此 collection 改为分步建 schema/index，并显式以
  `replica_number=1` 加载；新建失败只回滚本调用创建的空 collection，已有旧 schema
  仍只报错、不删除。
- 全量单测：`1810 passed, 19 skipped, 178 deselected, 1 xfailed`，pytest 8.62s；
  Ruff 与 Pyright 通过，Pyright 为 `0 errors`。

## 9. M3-T4 验证记录

- `KeywordSearchPort` 与 `BaseVectorStore` 保持独立；组合根只在实现具备关键词能力时
  将该窄 Port 注入 `RetrievalService`，三模式调用语义留到 M3-T6。
- InMemory 使用标准库实现确定性 BM25 参考语义；Milvus 使用原始 query text 与原生
  BM25 sparse field，供应商参数未穿透 Port。
- 两个实现共享 12 项契约；真实 Milvus 测试在 `zhihao_test_database` 全部通过
  （`12 passed in 60.74s`），覆盖中英文、隔离、过滤、top_k、空查询与错误语义。
- 验收后测试前缀 collection 残留为 0，数据库原有 collection 总数仍为 1。
- 过滤缺陷反向注入测试通过；全量单测为
  `1823 passed, 19 skipped, 190 deselected, 1 xfailed`，pytest 8.85s；Ruff 与 Pyright
  通过，Pyright 为 `0 errors`。

## 10. M3-T5 验证记录

- `engines/retrieval/fusion.py` 只依赖 `ports.SearchHit` 与标准库；独立进程导入
  `comet_rag.engines.retrieval` 不会加载 `pymilvus`。
- RRF 使用默认 `rrf_k=60` 和一基 rank，按 chunk id 跨通道去重；同一通道的重复 id
  只取首次排名，避免重复记录人为抬高融合分数。
- 输出保留每个通道的原始 rank/score；融合只使用排名，同分以 chunk id 稳定排序，
  不修改输入候选或共享 metadata。
- 临时把 rank 改成从 0 开始后，精确公式测试按预期失败；恢复后 12 项 RRF 单测通过。
- 全量单测为 `1849 passed, 19 skipped, 190 deselected, 1 xfailed`，pytest 9.26s；
  Ruff 与 Pyright 通过，Pyright 为 `0 errors`。

## 11. M3-T6 验证记录

- `SearchQuery.mode` 与 HTTP `SearchRequest.mode` 支持 dense、keyword、hybrid，默认
  dense；既有请求无需增加字段，原响应字段全部保留。
- keyword 模式不调用 embedding；hybrid 对两路并发召回，每路使用同一有界
  `fetch_k` 和结构化 filter，并在进入可选 reranker 前完成 RRF。
- 响应暴露实际 mode、channels，以及 vector/keyword/fusion 的原始分数和一基排名；
  `score` 仍表示当前最终排序分数，重排后保留此前诊断字段。
- 34 项服务/API 定向测试、719 项组合根/导入/分层/文档守卫测试通过。
- 全量单测为 `1860 passed, 19 skipped, 190 deselected, 1 xfailed`，pytest 8.84s；
  Ruff 与 Pyright 通过，Pyright 为 `0 errors`。

## 12. M3-T7 验证记录

- hybrid 并发收集两路结果：dense 单路失败时返回 keyword，keyword 单路失败时返回
  dense；两路都失败时抛 `HybridRecallFailed`，HTTP 映射为 503。
- `CollectionNotFound`、schema/维度、请求参数和明显编程错误直接传播；知识库与模型
  一致性守卫在启动通道前执行，不会被当作一次可恢复抖动。
- 显式 dense/keyword 模式不切换通道；缺少 `KeywordSearchPort` 返回安全的 503 配置错误。
- 新增结构化 `degradations`，与系统负载级别 `degraded` 分离；API 只返回 stage、
  reason 和异常类型，原始异常消息不出现在响应，完整异常链进入 warning 日志。
- hybrid 的 reranker 失败返回融合候选并保留 fusion 诊断；组合根复用同一窄 Port，
  既有生命周期测试证明 vector store 只关闭一次。
- 43 项服务/API 故障矩阵测试、665 项组合根/生命周期/分层保护测试通过；全量单测为
  `1869 passed, 19 skipped, 190 deselected, 1 xfailed`，pytest 9.02s。

## 13. 官方依据

- [Milvus Full Text Search](https://milvus.io/docs/full-text-search.md)
- [Milvus BM25 Function](https://milvus.io/docs/bm25-function.md)
- [Milvus RRF Ranker](https://milvus.io/docs/rrf-ranker.md)
- [Milvus Analyzer 选择](https://milvus.io/docs/choose-the-right-analyzer-for-your-use-case.md)
- [PyMilvus 兼容矩阵](https://milvus.io/api-reference/pymilvus/v3.0.x/About.md)
- [Milvus Releases](https://github.com/milvus-io/milvus/releases)
