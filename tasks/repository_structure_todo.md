# Repository Structure Normalization TODO

## RS-T0：规格与保护网

- [x] 冻结目录判定规则、迁移边界和兼容策略。
- [x] 补充新目录依赖守卫与旧导入身份测试。

## RS-T1：Persistence

- [x] 建立 `infrastructure/persistence/sql/`。
- [x] 建立 vector/task/knowledge-base store 子包。
- [x] 将 KnowledgeBase 领域对象与契约迁入 ports。
- [x] 保留 database/vectorstore/knowledge_base 旧路径兼容。

## RS-T2：Sources

- [x] 将 Local、HTTP、S3 Loader 统一迁入 `infrastructure/sources/`。
- [x] `AutoLoader` 作为无业务策略的组合适配器与来源实现同包。
- [x] 保留 `comet_rag.loaders` 稳定入口及旧路径兼容。

## RS-T3：Task execution

- [x] 将依赖 Redis 的 ARQ 实现迁入 `infrastructure/task_execution/`。
- [x] `tasks/` 保留任务内核、契约与无外部依赖的参考实现。

## RS-T4：Documents 与 API DTO

- [x] 收拢 DOCX 专属 converter/parser/cleaner。
- [x] 将 HTTP DTO 迁入 `api/schemas/`。
- [x] 保留旧路径兼容并验证 DOCX 快照不变。

## RS-T5：验收

- [x] 更新组合根、分层守卫和架构文档。
- [x] 运行 ruff、pyright、unit、integration 可用性检查。
- [x] 检查旧路径只包含兼容转发，不存在双份实现。
