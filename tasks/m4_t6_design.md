# M4-T6 Design：父子索引、Revision 与双存储一致性

> 状态：设计已完成，等待持久化变更确认
> 依据：`tasks/m4_spec.md` v1.7
> 范围：只冻结接口、schema、故障语义与迁移顺序；本文件不授权创建表或修改 Milvus 数据
> 最后更新：2026-09-16

## 1. 决策摘要

M4 后半段采用以下形态：

```text
NormalizedDocument
        │
        ▼
HierarchyBuilder (engines)        纯计算：先父后子，子块不跨父块
        │ ChunkHierarchy
        ▼
IndexPlanner (services)           物化 revision、稳定 ID 与相邻关系
        │ IndexPlan
        ├──────────────► DocumentStore     PostgreSQL：revision manifest + 父块正文
        │
        └── EmbeddingPort ───────► VectorStore       Milvus：可检索子块

RetrievalService
        ├── Vector/Keyword recall（强一致、有界 overfetch）
        ├── DocumentStore 批量判定 active revision
        ├── RRF / rerank
        └── DocumentStore 批量回填父块
```

核心决策：

1. PostgreSQL 的 `documents.active_revision_id` 是唯一可见性真相；Milvus 不维护
   `active=true` 镜像状态。
2. 每个 revision 的父块与子向量都使用含 revision 的新 ID；旧 active revision 在新版本
   完全写好前不变。
3. revision generation 在 PostgreSQL 行锁内单调分配。较早任务晚完成时不能覆盖较新的
   active revision。
4. Dense 与 BM25 在 RRF 前分别过滤非 active candidate；任何路径都不得先融合旧版本再
   判活。
5. revision-aware Milvus 搜索使用 `Strong` consistency。当前 `Session` 只保证同一客户端
   看见自己的写入，不能保证 API 进程立即看见 worker 写入。
6. GC 是可重试的清理，不参与激活事务。清理失败会增加存储和 overfetch 压力，但不会
   让旧 revision 重新可见。
7. 现有无 `document_revision` 的向量作为 legacy active 数据兼容；某 source 首次激活新
   revision 后，它的 legacy 向量立即变为不可见。其物理删除由经过版本验证的独立维护
   流程完成，不混入普通 revision GC。

## 2. 不采用的方案

### 2.1 Milvus `active=true` 翻标

拒绝。一次 source 更新包含多条 entity，启用新版本和禁用旧版本至少是两次批量写；
Milvus 不能与 PostgreSQL 共事务，并且跨请求 upsert 不提供原子切换。先关旧版会产生空窗，
先开新版会短暂重复，进程崩溃还会把临时状态永久留下。

### 2.2 继续覆盖稳定的 `source_id:index` ID

拒绝。Milvus 一批 upsert 与 PostgreSQL 父块写入不是同一个事务。覆盖到一半失败时，
旧文档会由新旧 chunk 拼成一份不可识别的混合版本，无法可靠回滚。

### 2.3 每个 source/revision 一个 partition

拒绝。知识库搜索需要同时覆盖大量 source，而每个 source 的 active partition 不同；查询
前必须先加载并传入一个可能无界的 partition 列表。partition 数量、装载和路由成本也会
随 source 数持续增长。

### 2.4 把所有 active revision 拼成一个 Milvus filter

拒绝。现有后端无关 `Filter` 只有等值、IN 与 AND。分别对 source 和 revision 使用 IN 会
产生错误的笛卡尔组合；扩展为数千个 `(source_id, revision)` OR 对既会让供应商语法穿透
Port，也会制造无界表达式。

### 2.5 把父块正文塞进 Milvus JSON

拒绝。父块不参与 ANN/BM25，放入 JSON 会复制大段正文、放大向量 collection 和网络返回，
还让关系型生命周期与供应商 schema 耦合。Milvus 只保留有界 `parent_id` 引用。

## 3. 纯计算模型

### 3.1 `HierarchyBuilder`

位置：`comet_rag/engines/indexing/`。它实现
`ChunkingStrategy[ChunkHierarchy]`，直接消费 `NormalizedDocument`，而不是把一组已经
切好的 child 倒推成 parent。

首版固定两层，并采用“先父后子”：

1. 先在每个 section/page 硬边界内使用 parent Chunker，首版 parent overlap 必须为 0；
2. 再对每个 parent 的正文调用 child Chunker；
3. child 的局部 span 映射回规范 Markdown 全局 span；
4. 每个 child 恰好属于一个 parent，绝不能跨 parent；
5. 父、子 ordinal 分别从 0 连续递增，相邻关系按全局 ordinal 生成；
6. 单个 child 超过 parent 目标大小时，由 parent Chunker 的硬上限处理，不能靠截断 child
   破坏正文。

```python
@dataclass(frozen=True, slots=True)
class HierarchyParent:
    ordinal: int
    text: str
    start_char: int
    end_char: int
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class HierarchyChild:
    ordinal: int
    parent_ordinal: int
    text: str
    start_char: int
    end_char: int
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ChunkHierarchy:
    parents: tuple[HierarchyParent, ...]
    children: tuple[HierarchyChild, ...]
```

`ChunkHierarchy` 不含 `kb_id`、`source_id`、revision、向量或数据库字段。关闭 hierarchy 时，
平坦 `ChunkDraft` 仍可进入 IndexPlanner，产生零父块的 revision plan。

### 3.2 `IndexPlanner`

位置：`comet_rag/services/indexing.py`。它把纯计算结果与来源身份组合成写计划：

```python
@dataclass(frozen=True, slots=True)
class PlannedParent:
    id: str
    ordinal: int
    text: str
    start_char: int
    end_char: int
    previous_id: str | None
    next_id: str | None
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PlannedChild:
    id: str
    ordinal: int
    parent_id: str | None
    text: str
    start_char: int | None
    end_char: int | None
    previous_id: str | None
    next_id: str | None
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class IndexPlan:
    revision_id: str
    source_id: str
    plan_sha256: str
    parents: tuple[PlannedParent, ...]
    vector_children: tuple[PlannedChild, ...]
```

- `revision_id` 使用一次生成并写入 Task context 的 UUID4 hex；重试必须复用，不能重生成。
- parent/child ID 分别为
  `sha256("{source_id}:{revision_id}:parent:{ordinal}")` 与
  `sha256("{source_id}:{revision_id}:child:{ordinal}")`。
- `plan_sha256` 覆盖 revision 以外的正文、span、关系和 metadata，用于拒绝“同 revision ID
  却换了一份内容”的错误重试。
- 只有 `vector_children` 进入 embedding；parent 永远不向量化。
- 平坦模式的 `parent_id` 为 `None`，仍使用 revision 写协议。

## 4. `DocumentStore` Port

位置：`comet_rag/ports/document_store.py`。它隔离 PostgreSQL 事务和父块生命周期，不进入
`engines`。建议接口冻结为：

```python
@dataclass(frozen=True, slots=True)
class RevisionKey:
    kb_id: str
    source_id: str
    revision_id: str


@dataclass(frozen=True, slots=True)
class RevisionDraft:
    key: RevisionKey
    plan_sha256: str
    parent_count: int
    child_count: int
    source: str
    file_type: str
    metadata: Mapping[str, object]
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class StoredParent:
    key: RevisionKey
    parent_id: str
    ordinal: int
    text: str
    start_char: int
    end_char: int
    previous_id: str | None
    next_id: str | None
    metadata: Mapping[str, object]


class DocumentStore(ABC):
    async def areserve_revision(self, draft: RevisionDraft) -> StoredRevision: ...
    async def aput_parents(
        self, revision: RevisionKey, parents: Sequence[StoredParent]
    ) -> None: ...
    async def amark_ready(self, revision: RevisionKey) -> StoredRevision: ...
    async def aactivate_revision(self, revision: RevisionKey) -> Activation: ...
    async def aget_active_revisions(
        self, kb_id: str, source_ids: Sequence[str]
    ) -> dict[str, str | None]: ...
    async def aget_parents(
        self, kb_id: str, parent_ids: Sequence[str]
    ) -> dict[str, StoredParent]: ...
    async def aabandon_revision(self, revision: RevisionKey) -> None: ...
    async def alist_cleanup_candidates(self, *, limit: int) -> list[RevisionKey]: ...
    async def adelete_inactive_revision(self, revision: RevisionKey) -> None: ...
    async def aclose(self) -> None: ...
```

`StoredRevision` 返回 key、generation、status 和计划计数；`Activation` 返回新旧 revision
以及是否为幂等重放。Port 定义 `DocumentStoreError`、`RevisionConflict`、
`RevisionNotReady`、`StaleRevision`、`ActiveRevisionDeletion`，基础设施连接错误保留原始异常
作为 cause。值对象使用 frozen/slots dataclass，不在 Port 引入 SQLAlchemy 或 Pydantic。

契约语义：

- `areserve_revision` 在 document 行锁内分配单调 `generation`；相同 revision + 相同
  `plan_sha256` 幂等返回，相同 ID + 不同计划拒绝。
- `aput_parents` 支持有界批量和幂等重放；同 parent ID 内容不一致必须拒绝。
- `amark_ready` 只允许 `staging → ready`，并核对父块实际条数。调用它之前 Service 必须用
  `VectorStore.acount({source_id, document_revision})` 核对子向量实际条数。
- `aactivate_revision` 在单个 PostgreSQL 事务中锁 document 与目标 revision；只允许 ready
  且 generation 大于当前 active generation 的 revision。它原子完成旧 active → retired、
  新 ready → active 和 active pointer 切换。
- 重试已经 active 的 revision 幂等成功；较老 generation 返回 `StaleRevision`，不能覆盖
  新版本。
- `aget_active_revisions` 必须单次批量查询并按 kb 隔离。value 为 `None` 表示该 source 尚
  无 revision active，供 legacy 兼容，不表示任意 revision 可见；返回映射必须包含每个
  请求的 source，不能用“缺 key”制造第三种语义。
- `adelete_inactive_revision` 必须拒绝删除 active；`retired` 与 `abandoned` 一旦进入便不能
  重新激活。
- PostgreSQL 与 InMemory 实现必须继承同一套契约测试。

revision 状态机：

```text
reserved/staging ── vectors + parents complete ──► ready ──► active
       │                                            │          │
       └──────────────────────► abandoned ◄─────────┘          └──► retired

abandoned / retired ── vector GC complete ──► delete revision + cascade parents
```

## 5. PostgreSQL schema

### 5.1 `documents`

| 列 | 类型 | 约束/作用 |
|---|---|---|
| `kb_id` | varchar(128) | FK `knowledge_bases(kb_id)` ON DELETE CASCADE，联合主键 |
| `source_id` | varchar(64) | 联合主键 |
| `active_revision_id` | varchar(64), nullable | 当前可见 revision；首次 revision 激活前为空 |
| `next_generation` | bigint | 行锁内递增，默认 0，禁止回退 |
| `source` | text | 最近激活版本的来源显示值 |
| `file_type` | varchar(32) | 最近激活版本格式 |
| `metadata` | jsonb | 有界文档级信息，不保存正文 |
| `created_at/updated_at` | timestamptz | 生命周期 |

主键：`(kb_id, source_id)`。`active_revision_id` 使用迁移末尾增加的可延迟复合外键，指向
同一 `(kb_id, source_id)` 的 revision，避免 active pointer 串到另一个租户或 source。

### 5.2 `document_revisions`

| 列 | 类型 | 约束/作用 |
|---|---|---|
| `revision_id` | varchar(64) | 主键 |
| `kb_id/source_id` | varchar | 复合 FK `documents` ON DELETE CASCADE |
| `generation` | bigint | 每 source 单调递增，`> 0` |
| `status` | varchar(16) | `staging/ready/active/retired/abandoned`，不用 PG enum |
| `plan_sha256` | char(64) | 幂等内容指纹 |
| `parent_count/child_count` | integer | 均 `>= 0`，ready 前核对 |
| `task_id` | varchar(64), nullable | 诊断与恢复，不作为任务外键 |
| `replaces_revision_id` | varchar(64), nullable | 激活时记录旧 active，便于审计 |
| `created_at/ready_at/activated_at/updated_at` | timestamptz | 恢复与 GC |

约束和索引：

- `UNIQUE(kb_id, source_id, generation)`；
- `UNIQUE(kb_id, source_id, revision_id)`，供 active pointer 复合外键使用；
- partial unique index：同一 `(kb_id, source_id)` 在 `status='active'` 时最多一行；
- partial GC index：`(status, updated_at)` WHERE status IN (`retired`, `abandoned`)；
- 状态 CHECK 使用 varchar 白名单，未来增加状态只改 CHECK，不使用难回滚的 PG enum。

### 5.3 `document_parents`

| 列 | 类型 | 约束/作用 |
|---|---|---|
| `parent_id` | varchar(64) | 主键 |
| `revision_id` | varchar(64) | FK revision ON DELETE CASCADE |
| `kb_id/source_id` | varchar | 查询隔离与复合一致性校验 |
| `ordinal` | integer | revision 内连续，`>= 0` |
| `text` | text | 父块正文 |
| `start_char/end_char` | integer | 左闭右开，`0 <= start < end` |
| `previous_id/next_id` | varchar(64), nullable | 父块相邻关系 |
| `metadata` | jsonb | heading/page 等父块事实 |
| `created_at` | timestamptz | 审计 |

索引：`UNIQUE(revision_id, ordinal)`、`INDEX(kb_id, parent_id)`。父块查询始终同时携带
`kb_id`，即使 ID 是哈希也不能把哈希当租户隔离。

## 6. Milvus metadata 与一致性

新子向量只增加 JSON metadata，不修改 collection schema：

| 字段 | 必填 | 作用 |
|---|---|---|
| `document_revision` | 是 | 与 PostgreSQL active pointer 比较 |
| `parent_id` | 层级模式是 | 回填父块；平坦模式为缺省/None |
| `previous_id` / `next_id` | 否 | 子块相邻导航 |
| `source_id` | 是 | 已有字段；批量判活和 revision 精确清理 |
| `chunk_index` | 是 | 已有字段；revision 内 ordinal |

不增加 `active`、generation 或父块正文。精确 GC 使用现有结构化过滤：

```python
{"source_id": source_id, "document_revision": revision_id}
```

revision-aware dense、BM25 search 以及激活前的 count 验证必须使用 Strong consistency。
这是 Milvus adapter 的配置/调用细节，不把供应商一致性参数加入 `VectorSearchPort`。
`max_revision_candidates` 默认 2048，必须小于 Milvus top-k 16384 硬上限。

现有 collection 不需要立即重建：metadata 是已有 JSON 字段。旧向量没有
`document_revision`，按 §7 的 legacy 规则继续工作。常规 GC 只按明确 revision 删除，
不会假设某个 Milvus 版本对“缺失 JSON key == null”的表达式行为；legacy 物理清理由独立
维护命令枚举 ID 或重建 active-only collection，并在 T10 对目标 Milvus 版本做集成验证。

## 7. Active revision 有界检索

### 7.1 可见性谓词

对每个 candidate：

```text
candidate 有 document_revision
    => 仅当 revision == active[source_id] 时可见

candidate 无 document_revision（legacy）
    => 仅当 active[source_id] is None 时可见
```

缺少合法 `source_id` 的 revision candidate 是损坏数据，拒绝返回并记录错误。DocumentStore
不可用时也不能绕过判活返回全部 candidate，否则一次 PostgreSQL 故障会泄漏已退休版本。

### 7.2 两次查询上界

每个召回通道独立执行：

1. `initial_limit = min(max(fetch_k * 4, 64), max_revision_candidates)`；
2. Strong consistency 搜索，批量读取候选 source 的 active revision 并过滤；
3. active candidate 已达到 `fetch_k`，或底层返回少于 limit，立即停止；
4. 否则只再查询一次 `max_revision_candidates`，按 ID 去重后重新判活；
5. 达到上限仍不足时返回较少结果，记录 `revision_candidate_cap` 诊断，不返回旧版本。

证明：单通道最多两次 ANN/BM25、最多检查 2048 个 candidate、每轮最多一次批量 active
查询，因此数据库往返最多两次、单次 source 数最多 2048，请求次数和内存都有固定上界；
可见性谓词保证 stale candidate 永远不会输出。病态情况下无法保证填满 `fetch_k`，这是
有界系统必须公开的质量退化，不能用无界翻页掩盖。

Hybrid 必须对 dense/keyword 两个列表分别判活后再做 RRF。否则旧 revision 在两个通道的
排名会污染 active child 的 fusion score，即使最后删掉旧结果也无法恢复正确名次。

### 7.3 父块回填

RRF/rerank 仍对子块执行。child 模式保持当前 top-k 行为；parent 模式遍历完整的已排序
候选列表，按首次出现的 `parent_id` 去重，批量读取这些 parent，再截取 top-k 个不同父块，
而不是先把 child 截成 top-k 后才去重。返回结果保留 `matched_child_id` 与 child score。
父块读取失败时记录降级并返回原子 child，不让读路径整体失败。缺少单个 parent 是数据
完整性错误：该项回退 child 并报警，不能静默丢结果。

## 8. 写入协议

1. 生成一次 revision ID 和 IndexPlan；Task context 只保存 revision ID、plan digest、计数和
   继续下游所需的窄 child payload，不重复保存父块正文或整个 IndexPlan；
2. `areserve_revision` 分配 generation；
3. 分批幂等写 parent；
4. 分窗口 embedding，并以 revision child ID 幂等 upsert Milvus；
5. Strong count 核对本 source/revision 的 child 数，DocumentStore 核对 parent 数；
6. `amark_ready`；
7. `aactivate_revision` 原子切换 PostgreSQL active pointer；
8. 返回成功前已保证新 revision 可由 Strong search 观察；
9. 最佳努力执行 retired/abandoned GC。失败只记录，状态行就是持久化 GC 队列；后续同
   source 入库和多进程 maintenance 都可重试。

任务重试从 Task context 取同一 revision ID 和窄 plan manifest。若更高 generation 已激活，
旧任务清理自己的 revision 并以 `superseded=true` 正常结束，不把“被更新版本取代”误报成
基础设施失败。

## 9. 故障与重试矩阵

| 断点/故障 | 对外可见版本 | 恢复动作 | 必须证明的不变式 |
|---|---|---|---|
| reserve 前失败 | 旧 active/legacy | 普通任务重试 | 没有孤儿状态 |
| reserve 后、parent 前崩溃 | 旧 active/legacy | 同 revision 重放或标 abandoned | staging 永不参与检索 |
| parent 批次写到一半 | 旧 active/legacy | 按 ID 幂等补齐 | 不出现半份父块可见 |
| embedding/向量批次失败 | 旧 active/legacy | 同 revision child ID 重放 | 不覆盖旧 active |
| 向量写完但 count 不符 | 旧 active/legacy | 补写；多出则拒绝并清理 | 不激活不完整 plan |
| ready 前后响应丢失 | 旧 active/legacy | mark-ready 幂等重试 | ready 本身不可见 |
| activation 事务失败 | 旧 active | PostgreSQL 回滚后重试 | active pointer 与状态同事务 |
| activation 提交后响应丢失 | 新 active | 重试识别 already-active | 不重复 generation/不回退 |
| 新任务先激活、旧任务后完成 | 新任务 revision | 旧任务得到 superseded 并 GC | generation 围栏阻止旧覆盖新 |
| 激活后进程立刻崩溃 | 新 active | retired 行由后续 GC 扫描 | GC 不影响提交成功 |
| 旧向量 GC 部分失败 | 新 active | 按 revision filter 重试 | stale 向量存在也不可见 |
| 旧 parent GC 失败 | 新 active | revision cascade 重试 | 旧 parent 不被新 child 引用 |
| DocumentStore 判活查询失败 | 不返回结果 | 请求失败/可重试 | 禁止 fail-open 泄漏 stale |
| 单个 parent 缺失/读取失败 | active child | 降级返回 child 并报警 | 检索仍可用且不伪造 parent |
| reranker 失败 | active child 原排序 | 沿用现有降级 | revision 过滤早于 rerank |
| worker/API 使用不同 Milvus client | 新 active | Strong search 等待可见 | 不因 Session 边界产生空窗 |
| 重复 GC | 新 active | delete 0 行视为成功 | cleanup 幂等且拒删 active |

## 10. 契约与测试计划

### DocumentStore 共享契约

- reserve generation 单调、相同 revision 幂等、不同 digest 冲突；
- parent 批量重放、内容冲突、ordinal 与租户隔离；
- staging/ready/active/retired/abandoned 合法与非法迁移；
- active 切换原子、同时最多一个 active、旧 generation 不能反杀新 generation；
- batch active lookup 的 legacy `None` 语义；
- parent batch lookup 不跨 KB；
- GC 只列出 retired/abandoned，删除 active 必须失败；
- InMemory 与 PostgreSQL 跑同一套测试，PostgreSQL 加并发事务用例。

### 双存储与检索测试

- 每个 §9 断点用故障注入覆盖，且反向移除 active predicate/generation fence 时测试会红；
- dense、keyword 在 fusion 前过滤，旧版本高分不能污染 RRF；
- legacy source 在首次新 revision 激活前可见，激活后不可见；
- 2048 个 stale candidate 后明确少返回且诊断，不发生第三次查询；
- 两个独立 MilvusStore client 验证写后 Strong 可见；
- parent 去重、缺失与存储故障均降级到 child；
- GC 中断后可重复，任何时刻 active revision 仍可检索。

## 11. 迁移、发布与回滚

### Upgrade

1. 先部署 revision-aware **读代码**，hierarchy/revision 写开关保持关闭；
2. Alembic 创建三张空表、CHECK、FK 与索引，不扫描或修改现有 Milvus；
3. 运行 InMemory/PostgreSQL contract、Milvus 双 client 和 legacy 兼容测试；
4. 再开启 revision 写入。Milvus 只增加 JSON metadata，无 collection schema migration；
5. 观察 stale ratio、overfetch cap、GC backlog、Strong search P95 后再默认开启 hierarchy。

配置约束：InMemoryVectorStore 配 InMemoryDocumentStore；Milvus 启用 revision 写时必须配
PostgresDocumentStore，禁止使用重启即丢 active pointer 的内存实现。

### Rollback

- **写开关开启前**：可以直接回滚代码；三张空表可安全 downgrade。
- **出现首个 revision 向量后**：不能直接回滚到 M3 二进制。旧 RetrievalService 不认识
  active revision，会把多个版本一起返回；旧稳定 ID 写入也会与 revision ID 并存。
- 正确回滚顺序：停止新入库 → 保留 revision-aware 读路径 → 导出/确认 active manifest →
  将每个 source 只重建为 legacy 单版本 collection（或恢复启用前备份）→ 验证不存在
  `document_revision` 向量 → 确认 GC backlog 为 0 → 再回滚代码和 drop 表。
- Alembic downgrade 应在表非空时主动拒绝；数据库迁移无法独自证明 Milvus 已清理，必须
  由显式运维检查命令出具结果后才能执行。

这是一次向前兼容、数据写入后非平凡回滚的变更。T7 开始前必须明确接受这一点，不能把
“drop 三张表”误写成完整回滚方案。

## 12. 实施切片

- **M4-T7**：值对象、DocumentStore Port、InMemory/PostgreSQL、迁移与共享契约；不改写入。
- **M4-T8**：HierarchyBuilder、IndexPlanner、revision Task context、双存储写入和故障矩阵。
- **M4-T9**：Strong revision-aware recall、有界判活、RRF 前过滤、父块回填与 GC。
- **M4-T10**：真实 PostgreSQL/Milvus 双 client、质量/延迟/存储评测、发布与回滚演练。

## 13. 决策门

进入 M4-T7 前需要确认：

1. 允许新增 `documents`、`document_revisions`、`document_parents` 三张 PostgreSQL 表；
2. 允许新向量 metadata 增加 `document_revision`、`parent_id`、相邻 ID；
3. 接受 revision-aware Milvus 检索使用 Strong consistency 及其延迟成本；
4. 接受写入 revision 数据后不能直接回滚到 M3，必须先重建 active-only collection；
5. 接受有界 overfetch 在 stale 极端拥塞时宁可少返回，也不泄漏非 active 版本。

## 14. 设计依据

- [Milvus consistency](https://milvus.io/docs/consistency.md)：Session 只保证同一客户端的
  read-your-writes；跨 worker/API 客户端的 revision 切换因此使用 Strong。
- [Milvus filtered search](https://milvus.io/docs/filtered-search.md)：现有 metadata 等值过滤
  可继续用于 source/revision 清理和核对，不把供应商表达式暴露给 Port。
- [Milvus upsert entities](https://milvus.io/docs/upsert-entities.md)：跨请求、多 entity upsert
  不构成与 PostgreSQL 一致的原子切换，因此拒绝 Milvus `active=true` 翻标。
- [Milvus limits](https://milvus.io/docs/limitations.md)：候选硬上限 2048 低于单次查询
  `topk` 16,384 的已知上限，并且仍由应用配置施加更小的资源预算。
