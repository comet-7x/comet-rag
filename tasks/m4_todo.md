# TODO: Comet-RAG M4（Chunking 与层级索引）

> 状态：M4-T5 已完成；下一项 M4-T6 schema/revision 设计决策门
> 规格：`tasks/m4_spec.md` v1.5
> 计划：`tasks/m4_plan.md` v1.5
> GitHub Issue：[#57](https://github.com/comet-7x/comet-rag/issues/57)
> 开发分支：`feature/m4-chunking`

## Phase 0：调研与契约

### M4-T1 — 开源调研、现状审计与基线（S）

**日期：** 09-14

- [x] 从 PR #56 合并后的最新 `develop` 创建 `feature/m4-chunking`
- [x] 对照 LangChain、LlamaIndex、Haystack、RAGFlow 的分块与层级设计
- [x] 审计当前 BaseChunker、Hook、Pipeline、Task context 与 VectorRecord 数据流
- [x] 识别 `list[str]`、计量单位、位置恢复与 overlap 可观察性问题
- [x] 明确语义分块属于 Service + EmbeddingPort，不是完整纯 Strategy
- [x] 编写 M4 spec/plan/todo 草案
- [x] 记录 develop 单测、Ruff、Pyright 与 core-only 基线
- [x] 用户确认后冻结 `tasks/m4_spec.md` v1.0
- [x] 建立 GitHub Issue 并回填链接

**验收：** 不照搬框架类名；每项采用/拒绝都有本项目边界依据，后半段有明确 schema
决策门。

### M4-T2 — ChunkDraft 与 ChunkingStrategy 契约（M）

**完成日期：** 09-14　**依赖：** M4-T1

- [x] 定义 frozen/slots 的 `ChunkDraft` 与同步 `ChunkingStrategy`
- [x] 定义 start/end、ordinal、metadata 和不可表示位置的语义
- [x] 定义 metadata 合并优先级与系统保留键
- [x] 为旧 `list[str]` ChunkHook 建立兼容适配器与弃用窗口
- [x] 增加 core-only、类型、不可变性、兼容与分层测试
- [x] 反向让 Strategy import service/infrastructure，确认 AST 守卫会失败

**验收：** 契约不含 source-specific ID、embedding、async 或供应商字段。

## Phase 1：平坦分块核心

### M4-T3 — Fixed / Recursive 与位置算法（M）

**完成日期：** 09-14　**依赖：** M4-T2

- [x] 实现 FixedSizeChunker 和 RecursiveChunker
- [x] 内部 split/merge 全程携带字符 span，不用事后 `find()`
- [x] 支持注入 LengthFunction，默认值和单位写入文档
- [x] 代码差异先统一为 profile，T4.1 再删除参数型 `CodeRecursiveChunker`
- [x] separator profiles 替代重复算法子类；发布前删除无调用方的代码语言子类
- [x] 覆盖 CJK、重复文本、连续分隔符、代码前缀、超长 token 与空输入
- [x] 明确并测试 best-effort overlap
- [x] 反向改为事后 `find()` 与丢分隔符实现，确认对应性质测试会红

**验收：** 大小、顺序、重建、位置与非空不变式全部成立。

### M4-T4 — Pipeline / Task 单链路迁移（M）

**完成日期：** 09-15　**依赖：** M4-T3

- [x] 建立共享 Chunking Service/映射函数
- [x] Pipeline 与 IngestRunner 统一消费 NormalizedDocument 和 ChunkDraft
- [x] Task context 跨 worker 保存有界、可序列化的 ChunkDraft 结构
- [x] ID、document/request/system metadata 合并只有一份实现
- [x] 旧自定义 ChunkHook 与公开 Chunker 示例继续工作并有迁移警告
- [x] 同步、异步、流式、任务入库的同源文档产物一致

**验收：** 两条入口不再各自拼 metadata/ID；失败重试仍从 chunking/indexing 正确续跑。

### M4-T4.1 — 原子 Chunker 与参数画像收敛（M）

**完成日期：** 09-15　**依赖：** M4-T4

- [x] 明确当前只有 FixedSize、Recursive 两种原子分块方式，后续按真实算法扩展 2+N
- [x] 原子 Chunker 接收 `str`，可脱离 NormalizedDocument 和 Pipeline 单独使用
- [x] ChunkingStrategy 保留为基于 Chunker 的文档级纯计算编排
- [x] 格式和代码语言差异改为不可变 ChunkProfile
- [x] 删除 BaseChunker、RecursiveCharacterTextSplitter 和所有参数型 Chunker 类
- [x] 覆盖 separator start/end、非法值、连续分隔符及无匹配 separator 的 overlap
- [x] 迁移 Hook、公开示例与原有不变式测试

**验收：** 运行时代码只有两种原子 Chunker；参数画像不产生新类型，文档级 Strategy
与原子算法使用不同的最小输入契约。

### M4-T4.2 — Chunker 目录边界清理（S）

**完成日期：** 09-15　**依赖：** M4-T4.1

- [x] 所有 separator 常量改为不可变元组，避免全局参数被调用方修改
- [x] `engines/chunkers/types.py` 只保留纯切分产物 `ChunkDraft`
- [x] metadata 优先级、系统保留键和索引字段归还 `services/chunking.py`
- [x] metadata 物化测试迁到 Service 测试，分块契约测试不再感知入库字段
- [x] 明确 `NormalizedDocument` 由文档级 Strategy 消费，而非限制原子 Chunker

**验收：** 原子算法目录不拥有来源、知识库、revision 或 parent 等物化概念；公开
separator 配置不可被原地修改。

### M4-T5 — Markdown / Page 结构感知（M）

**完成日期：** 09-15　**依赖：** M4-T4.2

- [x] 从标题与页面用例反推最小 DocumentBlock，并定义 span 校验
- [x] Markdown 分析器识别 ATX/Setext 标题路径、代码围栏和结构边界
- [x] MarkdownSectionStrategy 编排原子 Chunker 并保留 heading_path
- [x] PageChunkingStrategy 只消费真实页事实，缺页行为显式
- [x] 决定并实现 MinerU 页内容到规范 Markdown page span 的无搜索映射
- [x] DOCX/PDF/Markdown 固定样本覆盖 metadata 传播和边界完整性
- [x] 不加入 bbox/资产等没有当前消费者的字段

**验收：** 结构由 Extractor/Normalizer 提供，Chunker 只消费，不重新猜文档事实。

## Phase 2：层级索引决策门

### M4-T6 — IndexPlan、schema 与 revision 设计（M）

**依赖：** M4-T5　**执行后续前需用户确认**

- [ ] 定义两层 IndexPlan、父子/相邻关系、向量化与回填集合
- [ ] 定义 DocumentStore 窄 Port、错误、资源和隔离语义
- [ ] 给出 PostgreSQL 表、唯一键、索引、迁移与回滚 SQL 设计
- [ ] 给出 Milvus metadata 新增字段、兼容和旧数据策略
- [ ] 手写双存储各断点的成功/失败/重试矩阵
- [ ] 证明 active revision 检索可在有界 overfetch 下实现
- [ ] 获得修改持久化 schema 与 metadata 约定的明确确认

**验收：** 未确认前不创建迁移、不改生产写路径；无法可靠过滤 revision 时停止重设计。

## Phase 3：父子索引实现（通过决策门后）

### M4-T7 — DocumentStore 契约与实现（M）

**依赖：** M4-T6

- [ ] InMemory 与 PostgreSQL 实现同一套 DocumentStore 契约
- [ ] 覆盖 KB/source/revision 隔离、幂等、激活、批量读取和回收
- [ ] 数据库不可用时 integration skip，不 fail
- [ ] 清表复用统一 fixture，不拼裸 TRUNCATE
- [ ] 契约期望独立手写并反向注入隔离缺陷

### M4-T8 — revision 双存储入库（M）

**依赖：** M4-T7

- [ ] revision 相关稳定 ID 与 IndexPlan 写入
- [ ] 父块、子向量、激活与旧版本回收按规格排序
- [ ] 任一阶段失败只回滚本 revision，旧 active 数据仍可查询
- [ ] 重试幂等，不产生重复可见记录
- [ ] Task progress/context 保持有界并支持断点续跑

### M4-T9 — 父块回填与检索兼容（M）

**依赖：** M4-T8

- [ ] dense/keyword/hybrid 命中子块后批量读取父块，禁止 N+1
- [ ] 支持返回 child 或 parent 的显式查询选项，默认行为兼容
- [ ] parent 缺失时按读路径规则降级并记录诊断
- [ ] reranker 的输入粒度与去重顺序有明确测试
- [ ] 旧平坦记录仍可检索，不伪造 parent

## Phase 4：出口验收

### M4-T10 — 评测、文档、完整质量门与 PR（M）

**依赖：** M4-T9；若后半段拆 PR，则 T5 先独立完成一次出口验收

- [ ] 固定样本比较 recursive/markdown/page/hierarchy
- [ ] 记录块长分布、边界完整性、hit@k、延迟、索引条数与存储增量
- [ ] Pipeline、配置、架构、目录、部署与迁移文档更新
- [ ] unit < 10s，core-only、integration、e2e、benchmark、Ruff、Pyright 全绿
- [ ] 创建面向 `develop` 的 PR，并完成 AI Bot 增量评审

## 提交与 PR 边界

T1～T5 每项至少一个 Conventional Commit，聚焦 Chunking；T6 只提交设计。通过决策门
后，从最新 develop 创建 `feature/m4-hierarchical-indexing` 实现 T7～T10，避免算法
重构与数据库迁移出现在同一个不可回滚 PR。
