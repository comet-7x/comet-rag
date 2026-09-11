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

`RetrievalService` 使用窄的 `VectorSearchPort` 与 `KeywordSearchPort`；入库、删除和
计数仍使用完整 `BaseVectorStore`。这样 Service 不再依赖 infrastructure，且读路径
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

## 4. 成功标准

### S1 — 分层与兼容

- [ ] `services/` 不再 import `infrastructure.vectorstore`，AST 分层守卫覆盖该规则。
- [ ] 旧 vectorstore import 路径仍可用，且指向同一契约对象。
- [ ] core-only 安装可导入 RRF 与检索 Port，不加载 `pymilvus`。
- [ ] `SearchQuery` 默认 dense，既有检索测试和 API 响应字段不回退。

### S2 — BM25 与 schema

- [ ] Compose 与 PyMilvus 使用匹配的 2.6 版本线。
- [ ] 新 collection 包含 analyzer、BM25 function 和 BM25 sparse index。
- [ ] 旧 schema 被明确拒绝，测试证明不会自动删除已有 collection。
- [ ] 中文术语、英文标识符和 metadata filter 的真实 Milvus 查询均命中。

### S3 — Port 与融合

- [ ] InMemory 与 Milvus 通过同一 KeywordSearchPort 契约。
- [ ] RRF 覆盖去重、单路缺失、并列排序、输入不变性与非法参数。
- [ ] 反向注入错误实现，确认契约和 RRF 测试确实会失败。
- [ ] dense、keyword、hybrid 三种模式的候选数和分数语义稳定。

### S4 — 降级与装配

- [ ] 单路失败只降级该路，两路失败才使 hybrid 查询失败。
- [ ] reranker 失败返回融合结果，并记录日志与响应诊断信息。
- [ ] 配置、组合根、API schema 和关闭顺序完成，路由不自行 new 资源。

### S5 — 真实链路与质量

- [ ] E2E 覆盖“精确术语靠 BM25、语义改写靠 dense、hybrid 合并两者”。
- [ ] 集成环境不可用时 skip，不 fail；真实 Milvus 可用时验证 analyzer 与混合链路。
- [ ] 记录 dense/keyword/hybrid 的命中、延迟和候选规模，不用单个样本宣称质量提升。
- [ ] 默认 `uv run pytest` 仍小于 10 秒，Ruff、Pyright、core-only、integration、e2e 全绿。

## 5. 实施顺序

1. 检索值对象与 Port 下沉，建立兼容导出和契约测试。
2. 对齐 Milvus/PyMilvus 版本，实现并验证 BM25 schema v2。
3. 实现 InMemory/Milvus keyword search。
4. 实现纯 RRF Strategy。
5. 改造 RetrievalService、API、配置与组合根。
6. 完成降级、真实集成、E2E、基准和文档验收。

每项至少一个 Conventional Commit。collection schema 变更不得与 Port 迁移混在同一提交。

## 6. M3-T1 验证记录

- 基线：`1786 passed, 19 skipped, 177 deselected, 1 xfailed`，pytest 9.38s。
- Ruff、Pyright 通过，Pyright 为 `0 errors`。
- 本地 PyMilvus 3.0.1 可离线构造 analyzer、BM25 Function 与 BM25 index 参数。
- Docker daemon 未运行，未执行真实 Milvus 2.5.4 验证；版本对齐后的真实验证是 T3 硬门槛。

## 7. 官方依据

- [Milvus Full Text Search](https://milvus.io/docs/full-text-search.md)
- [Milvus BM25 Function](https://milvus.io/docs/bm25-function.md)
- [Milvus RRF Ranker](https://milvus.io/docs/rrf-ranker.md)
- [Milvus Analyzer 选择](https://milvus.io/docs/choose-the-right-analyzer-for-your-use-case.md)
- [PyMilvus 兼容矩阵](https://milvus.io/api-reference/pymilvus/v3.0.x/About.md)
- [Milvus Releases](https://github.com/milvus-io/milvus/releases)
