# 阶段 0 验收记录

本页记录阶段 0 完成当时的状态；后续功能与当前验收结论见 [验证记录](validation.md)。

2026-10-29，按本地 v1.3 计划在 `refactor/comet-rag-v2` 执行清场与新版骨架建设。阶段 0 已完成，A–F 的解析、模型、存储与完整 Pipeline 尚待实现。

## 清理与保留

- 移除当前分支的旧接口、库门面、服务、任务、配置和整个旧包布局；无兼容别名或嵌套旧系统。
- 成套移除旧专属测试、Hook conftest、迁移链、应用配置、历史权威规格和使用文档。旧实现与历史说明由 `refactor/enterprise-reliability` 保留。
- 阶段开始与结束核对：V2 HEAD 仍为 `75d12489bb6ca96746b370d06623cfe85a64fa41`，旧分支仍为 `ef5e94cdeaafb836ffae03d57063911f6cae5ba1`。未改分支引用，未 fetch、暂存、提交或推送。
- 精确清单覆盖 262 个旧仓库文件；逐个核对未修改及 Git 保存位置。直接删除被自动审批拦截后，改为可逆移动至忽略目录 `tmp/phase0-923nbiks/original-files`，并保留 `cleanup.json`。备份不进入源码、测试收集或分发包。另移除 7 份旧解析器 JSON 快照，内容仍在 Git 中。
- 用户原有 `/tmp`、`/temp`、`/draft` 忽略项和 `.DS_Store` 保留；`.gitignore` 同时修正旧说明、允许新版 AGENTS 入库并补末尾换行。未操作其他 worktree、本地私有配置、资料或外部数据。

## 新版骨架

- 包版本 `0.2.0.dev0`，根包无旧导出；仅在实际有代码的 `documents.parsers` 与 `retrieval` 建立模块。
- ZIP/XML 预算检查迁入 `documents.parsers.archive`，保留压缩规模、成员数、XML 元素/文本限制与安全解析；预算本身拒绝布尔值、非正数和非有限值。
- RRF 迁入 `retrieval.fusion`，直接接收已排序单元 ID；保留一基排名、去重、各通道贡献和稳定同分次序，重复 ID 不占用后续名次。
- 保留 7 类 DOCX 生成样例及文本层/扫描两类 PDF 样例，独立核对原始结构；未保留旧产品 DTO 快照。
- 重写包元数据、锁文件、Makefile、CI、README、当前架构与开发说明，并加入当前 worktree 的 V2 AGENTS。运行依赖仅为当前代码使用的 lxml；python-docx 仅在开发组，后端/模型 extras 在实际适配器加入时声明。
- Compose 改用 `comet-rag-v2` 项目、独立命名卷和回环端口；保留 PostgreSQL、Milvus 及其 etcd/MinIO 依赖。未启动容器。

## 验证与边界

- 新 worktree 的独立 `.venv` 执行锁定依赖安装、Ruff、Pyright、测试收集与单元测试；44 个行为/来源夹具用例通过。
- 当前源码覆盖率 98%（111 条可执行语句）；只衡量上述少量迁入代码，不代表 RAG 功能完成。
- 在临时副本中引入错误的一基排名和移除压缩限额，对应测试均失败；工作源码未被污染。
- wheel 与源码包构建成功；检查分发文件不含旧包、旧入口、本地备份、私有配置、资料或过期快照。仅安装 wheel 与 lxml 的独立环境可导入并运行基础行为，旧模块路径不存在。
- Compose 配置、现行 Markdown 文件链接、CI/钩子 YAML 和锁文件一致性检查通过。提交钩子首次仅因修复 `.gitignore` 末尾换行失败；随后对全部现行文件（含未暂存的新文件）重跑，适用钩子全部通过，包括安全检查与单元测试。

真实 PostgreSQL/Milvus、MinerU、模型及四种文本/图片组合均未验收，远端 CI 未执行。当前版本无法完成文档入库和搜索；下一步为阶段 A 的契约、真实样例与隔离后端准备。
