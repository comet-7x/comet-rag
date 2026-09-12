# Repository Structure Normalization Specification

> 状态：已冻结，实施中
> 基线：`develop` @ `db1602c`
> 范围：只调整模块归属、导入路径与公开兼容入口，不改变运行时行为

## 1. 背景

当前目录同时按架构层、业务能力和运行进程分类，导致 Loader、持久化和任务适配器
分散。M4 将新增 `IndexPlan`、`DocumentStore` 与父子索引；若不先统一归属，新能力
会继续复制现有歧义。

本重构是 M4 的前置工作，不改变已经冻结的 M4 路线：开源调研、Chunk 契约、
切分算法、结构感知切分、IndexPlan、DocumentStore、可靠入库、父块回填、兼容
装配和质量评测。

## 2. 目录判定规则

1. `core/` 只放零项目依赖的横切基础能力。
2. `ports/` 只放跨层契约、契约词汇和值对象。
3. `engines/` 只放不访问远程服务或中间件的库内算法。
4. `services/` 编排 Port 与 Engine，不选择具体供应商。
5. `infrastructure/` 只放外部系统、持久化和执行机制的适配器。
6. `composition/` 是唯一选择并实例化具体实现的位置。
7. `api/`、`workers/`、`cli.py` 是进程入口，不实现业务规则。
8. 一个实现只有一个规范位置；旧路径只能是无逻辑的兼容转发层。

## 3. 关键决策

### RS-D1：来源加载统一为基础设施适配器

Local、HTTP 与 S3 都访问进程外资源，统一进入 `infrastructure/sources/`。
`SourceLoaderPort` 留在 `ports/source.py`；纯路由组合 `AutoLoader` 与适配器同包，
准入策略和业务调用编排留在 services。`comet_rag.loaders` 仅保留稳定公共入口。

### RS-D2：持久化使用统一父目录

`database/` 实际只表示 SQLAlchemy/PostgreSQL，名称过宽。关系数据库公共设施、
VectorStore、TaskStore、KnowledgeBaseRepository 与未来 DocumentStore 统一归入
`infrastructure/persistence/`，再按能力分包。

VectorStore 不属于关系型 `sql/`，但与 SQL Repository 一样属于 persistence。

### RS-D3：领域对象与存储实现分离

`KnowledgeBase`、Repository 契约及错误进入 `ports/knowledge_base.py`。内存与
PostgreSQL 实现分别进入 persistence；services 不再 import infrastructure。

### RS-D4：任务内核与执行适配器分离

`tasks/` 保留模型、状态机、Store/Executor 契约、Runner、通用 TaskService，
以及不依赖外部系统的 InMemoryTaskStore / InProcessExecutor 参考实现。
PostgreSQL TaskStore 进入 persistence，依赖 Redis 的 ARQ 实现进入
`infrastructure/task_execution/`。

### RS-D5：DOCX 专属逻辑垂直收拢

只将 DOCX 专属 converter/parser/cleaner/OMML 迁入 `engines/documents/docx/`；
能被其他格式复用的工具继续留在公共能力目录。该迁移不得改变快照。

### RS-D6：HTTP DTO 归 API 所有

当前 `schemas/` 仅包含 HTTP 请求与响应模型，迁入 `api/schemas/`。领域对象和任务
模型不得移入 API。

### RS-D7：Pipeline 编排与公共便捷装配分离

可测试、可注入的 Pipeline 用例进入 `services/pipeline.py`，只依赖 Port 与 Engine；
`comet_rag/pipeline.py` 作为库用户入口，负责缺省 Local/HTTP Loader 的便捷装配。
旧 `engines.pipelines.Pipeline` 继续指向公共入口，但 `engines` 内部实现不再反向依赖
基础设施。

### RS-D8：按适配能力替代泛化 Provider 桶

Embedding、Reranker 与 Vision 是模型适配器，统一归入 `infrastructure/models/`；
MinerU 实现 `DocumentExtractorPort`，归入 `infrastructure/extractors/`。共享的图片
引用与线路格式工具跟随模型目录，不再裸露在泛化的 `providers/` 根层。旧
`infrastructure.providers` 只保留兼容入口。

## 4. 兼容策略

- 旧公开导入路径保留对象别名，类身份必须保持一致。
- 兼容模块不得新增业务逻辑或复制实现。
- 新代码与组合根只使用新规范路径。
- 每条兼容路径都由 import 测试覆盖，并在后续版本统一决定移除期限。
- 默认配置、HTTP 契约、数据库 schema、向量 collection schema 均不改变。

## 5. 验收标准

- Local、HTTP、S3 实现只有一个规范目录。
- 所有持久化实现位于 `infrastructure/persistence/`。
- `services/` 不 import 具体持久化、模型或来源适配器。
- `tasks/` 不 import SQLAlchemy、ARQ、Redis 或 infrastructure。
- `engines/` 不 import infrastructure/services/tasks。
- 旧导入路径继续可用且对象身份不变。
- 单元测试、分层守卫、pyright 与 ruff 全部通过。
- 不修改数据库和 Milvus schema，不新增依赖，不开始 M4 实现。
