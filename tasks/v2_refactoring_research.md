# Comet-RAG V2：重构技能调研与启动方案

> 调研日期：2026-10-22（Asia/Shanghai）
> 仓库基线：`refactor/enterprise-reliability`，`11c099bb0e553ea304c121f00a6eb5d44f7713e4`
> 状态：调研完成；下文保留调研时的基线与建议，实际实施进度见 [V2 首批记录](v2_plan.md)。
> 用户方向：架构与可维护性优先；保持怀疑精神；允许可靠性改造和破坏性变更，以优雅、可读的代码为目标。

## 1. 结论

建议采用经过项目适配的组合：**影响范围分析 + 可验证的分批计划 + 局部 Python 分析 + 独立评审**。

- `thoreinstein/refactor` 提供分析结构：位置、调用方、风险、测试与顺序。
- Superpowers 的 `writing-plans`、`requesting-code-review`、`verification-before-completion` 提供计划与验收纪律；执行阶段参考 `executing-plans` 的连续推进方式。
- Python skills 中选取 `py-complexity`、`py-code-health` 的分析方法，作为热点线索。
- comet-rag 自己的规格、分层、安装边界、契约测试和注释标准决定最终实现。

整包启用会引入重叠的编排流程、工具链偏好和审批步骤。更适合先形成项目专用的工作约束，按批次调用需要的方法。本次只读调研了上游内容，没有安装 skills、执行其安装脚本或修改开发工具配置。

## 2. 上游核验与采用方式

### Superpowers

本次核验版本为 [v7.0.1](https://github.com/obra/superpowers/releases/tag/v7.0.1)，固定提交 `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b`。下面链接均固定到该提交，避免后续上游变化混入本次结论。

| Skill | 实际作用 | 项目采用方式 |
|---|---|---|
| [writing-plans](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/writing-plans/SKILL.md) | 基于规格明确文件职责、任务接口、测试命令、全局约束及评审重点；要求计划审阅 | 优先采用计划结构，文件放 `tasks/`；每批写清行为变化、调用方迁移、失败语义和验收 |
| [executing-plans](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/executing-plans/SKILL.md) | 当前版支持本会话逐任务连续执行，以 brief/ledger 保持上下文，最终独立评审 | 采用连续执行和简短决策记录；当前版本已不是常见旧介绍中的“三任务一暂停” |
| [requesting-code-review](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/requesting-code-review/SKILL.md) | 独立 reviewer 对照需求和 diff 评审 | 每个有意义的重构批次审查；传完整批次基线及未提交 diff，避免机械用 `HEAD~1` 漏审 |
| [verification-before-completion](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/verification-before-completion/SKILL.md) | 以实际命令、退出码和输出支撑完成声明 | 优先采用；区分 unit、真实后端、core-only、覆盖率和性能的证据范围 |
| [subagent-driven-development](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/subagent-driven-development/SKILL.md) | 每任务独立实现与评审，附带任务包、脚本及模型选择规则 | 高风险任务可以增加独立审查；执行方式按当前工具能力和授权调整，不必每个机械改名都走完整流程 |

具体适配：

1. “按职责拆分”在项目已有合法分层内实施。纯计算、外部适配、业务编排和资源装配各自有可执行的边界。
2. 现有行为的特征化测试可以先通过；新增守卫仍须注入对应缺陷验证。不能机械地为满足上游 TDD 文案删除既有实现。
3. 已授权的工作连续完成。上游默认计划确认、分支收尾菜单等不能取代当前用户授权，也不能增加无意义的反复确认。
4. 保留 uv、Ruff、Pyright 和现有 CI。上游 worktree 的 Poetry 示例不适用于这里。
5. 若以后安装或维护适配版，应保留技能引用的模板、references 和脚本，固定版本并注明改动；只复制 `SKILL.md` 可能缺失依赖。

上游 [using-superpowers](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/using-superpowers/SKILL.md) 有很强的自动触发和流程串联要求，因此“安装整包”会改变日常协作方式，不能把它理解成仅增加几个模板。

### Python Refactoring Skills

本次核验提交为 [`b08357109fe81821decc100c8ea73ea2e3add08b`](https://github.com/l-mb/python-refactoring-skills/commit/b08357109fe81821decc100c8ea73ea2e3add08b)，提交日期为 2026-03-02。仓库元数据更新时间不能代替代码维护时间。

| Skill | 核验结果 | 项目采用方式 |
|---|---|---|
| [py-refactor](https://github.com/l-mb/python-refactoring-skills/blob/b08357109fe81821decc100c8ea73ea2e3add08b/skills/py-refactor/SKILL.md) | 编排安全、测试、重复、复杂度、现代化和 hooks；默认加入多种开发工具 | 借鉴检查清单，避免与 Superpowers 同时充当总编排器 |
| [py-complexity](https://github.com/l-mb/python-refactoring-skills/blob/b08357109fe81821decc100c8ea73ea2e3add08b/skills/py-complexity/SKILL.md) | Radon/Lizard 等定位高复杂度和大文件 | 用于挑选审查对象；函数行数、CC、MI 都不能单独证明设计缺陷或重构成功 |
| [py-code-health](https://github.com/l-mb/python-refactoring-skills/blob/b08357109fe81821decc100c8ea73ea2e3add08b/skills/py-code-health/SKILL.md) | Vulture 查未使用代码、Pylint 查重复 | 对公开 API、CLI、路由、worker 注册、Protocol 实现逐一复核；仓库内无调用方不代表库用户不用 |
| [py-quality-setup](https://github.com/l-mb/python-refactoring-skills/blob/b08357109fe81821decc100c8ea73ea2e3add08b/skills/py-quality-setup/SKILL.md) | 默认 Python ≥3.13、mypy + basedpyright，还配置 Claude 权限与全局 Stop hook | 本项目只做缺口审计；已有 uv/Ruff/Pyright/pytest，原配置不适合直接执行 |
| [py-modernize](https://github.com/l-mb/python-refactoring-skills/blob/b08357109fe81821decc100c8ea73ea2e3add08b/skills/py-modernize/SKILL.md) | 要求 Python 3.13 目标与工具链现代化 | 语言最低版本单独评估实际收益；V2 的架构目标本身不构成升级理由 |

需要纠正原推荐中的一条信息：**`py-test-quality` 的状态自相矛盾**。[README](https://github.com/l-mb/python-refactoring-skills/blob/b08357109fe81821decc100c8ea73ea2e3add08b/README.md) 写 `wip`，但 [SKILL.md](https://github.com/l-mb/python-refactoring-skills/blob/b08357109fe81821decc100c8ea73ea2e3add08b/skills/py-test-quality/SKILL.md) 和编排表写 `stable`。应记录这一冲突，不能仅凭任一标签判断成熟度。

工具示例也需校订，不能复制后直接作为质量门：

- `lizard -C 15` 的阈值是 CCN（圈复杂度），不能当成认知复杂度测量。[Lizard 官方说明](https://github.com/terryyin/lizard#readme)
- `radon mi -n B` 筛的是 MI 等级，不能等同于 MI <65。[Radon 官方文档](https://radon.readthedocs.io/en/latest/commandline.html#the-mi-command)
- Pylint 重复代码长度选项是 `--min-similarity-lines`，技能中的 `--duplicate-code-min-lines` 不应照用。[Pylint 官方选项](https://pylint.readthedocs.io/en/stable/user_guide/configuration/all-options.html#min-similarity-lines)
- `from __future__ import annotations` 没有因为 Python ≥3.11 就变成无意义语句；保留项目现有约定。[Python 官方说明](https://docs.python.org/3/library/__future__.html)

### thoreinstein/refactor

本次仓库 HEAD 为 `08388dc947a23e4fad3a77af95f48bf8800eea6d`；技能正文声明版本 `1.0.0`，其最近修改提交为 `119c1c2044f0e25f8131585426356c08498b4121`（2026-01-20）。

[技能正文](https://github.com/thoreinstein/agents/blob/08388dc947a23e4fad3a77af95f48bf8800eea6d/skills/refactor/SKILL.md) 确实只做分析，要求每项建议包含调用影响、风险等级、测试影响和执行顺序。[报告模板](https://github.com/thoreinstein/agents/blob/08388dc947a23e4fad3a77af95f48bf8800eea6d/skills/refactor/references/templates/refactor-analysis.md) 简洁，适合用于 V2 每批开始前的分析。

它没有 Python 专项分析器或 comet-rag 的业务知识；“只分析”也意味着它本身不能承担后续实施与验收。价值在于约束建议必须有证据。

## 3. 当前项目真正的起点

本次读了规格、架构计划、可靠性计划、M4 计划、注释标准、CI、关键实现与测试。以下是已观察事实，不能与 V2 建议混为一谈：

| 事实 | 本地依据 |
|---|---|
| M1～M3 已完成；M4 已有算法、DocumentStore、prepare-to-ready、跨 worker 续跑与读侧切片 | `tasks/spec.md:61`；`tasks/enterprise_reliability_plan.md:204` |
| DocumentStore 已有激活原语；生产入库的 activation、superseded、GC 与收尾闭环，以及完整 M4 验收仍未完成 | `tasks/enterprise_reliability_plan.md:222`；`comet_rag/services/revision_context.py:259` |
| revision-aware 读开关默认关闭；生产仍注册旧 IngestRunner | `comet_rag/config/schemas.py:577`；`comet_rag/composition/context.py:135` |
| services 已被禁止导入任何 infrastructure；BaseVectorStore 已在 ports | `tests/unit/test_layering.py:273`；`comet_rag/ports/vector_store.py:117` |
| 已有 Ruff、Pyright、契约、安装边界、内存 E2E、真实 PG/Redis/Milvus CI | `pyproject.toml`；`.github/workflows/tests.yml` |
| 包版本仍为 0.1.0；“V2”尚不自动等于发布版本号 2.0.0 | `pyproject.toml:3` |

文档漂移已经需要处理：

- `AGENTS.md:14` 还停在 M2；其 TaskStore 契约数量也未同步。本次 AST 统计为 54 个 `test_*` 函数，参数化后的用例数另计。
- `tasks/architecture_plan.md:137` 还将向量存储 Port 下沉列为未来事项，而代码已经完成。
- `tasks/spec.md` 的运行命令仍包含直接 uvicorn，现行入口约定是 `comet-rag serve`。

这类漂移会让代理重复重构已完成的部分，应先校准现状，再写 V2 目标。

## 4. 本次实测基线

使用现有环境，以 `UV_CACHE_DIR=/tmp/comet-rag-v2-uv-cache uv run --no-sync --offline` 执行，避免环境同步修改依赖。

| 检查 | 本次结果 |
|---|---|
| `pytest tests/unit -q --durations=10` | 2279 passed、20 skipped、1 xfailed；11.28s |
| 针对耗时和 skip 的复核：`pytest tests/unit -q -rs --durations=5` | 同样计数；9.71s |
| `pytest tests/e2e -m e2e -q` | 49 passed；2.00s |
| `ruff check .` | 通过 |
| `pyright` | 0 errors、0 warnings |

20 个 skip 分别是 1 个需显式启用的本地真实 DOCX 冒烟，以及 19 个不引用 comet-rag 符号的文档代码块。已有 xfail 位于 OMML 测试。

单测耗时接近预算且有超 10s 波动；复跑通过不能证明预算稳定满足。历史可靠性记录也出现相同现象。本次没有重新运行真实后端集成、隔离 core-only 安装、性能评测或三层覆盖率合并，已有历史通过记录不算本次验收。

## 5. 把“优雅、可读”变成可评审标准

用户已允许必要的破坏性变更，因此兼容旧签名不是最高目标。每项改动仍应解释新设计为何更容易理解，并列出调用方和数据的迁移方式。

1. **职责清楚**：函数的名称、输入输出和副作用能说明它为什么存在；纯计算与 I/O 的边界可见。
2. **控制流直接**：读核心路径时能看到关键顺序、失败处理和资源归属；避免为缩短函数而制造多层跳转。
3. **抽象有消费者**：Port 隔离真实外部能力，Strategy 表达真实算法替换；新建通用框架须有当前用例支撑。
4. **状态尽量少**：同一事实有明确权威位置；重试、移交、激活与回收有可解释的状态和所有权。
5. **代码显式且有类型**：优先领域词汇与窄接口；不要为降低重复率用动态字段循环替代清晰的类型关系。
6. **注释解释约束**：继续遵循 `docs/comment_style.md`，解释契约、边界、为什么；长设计放文档。
7. **怀疑结论并验证**：工具告警只是候选；先读调用方和反例，再决定提取、内联、保留重复或删除。
8. **行为变化可追踪**：纯结构调整、可靠性修复、公开 API 变更在可独立验证的批次中说明；破坏兼容时同步更新使用方、示例与迁移说明。

保留“库 + 服务”、引擎纯计算、安装边界、Milvus + InMemory、进程级有界并发等产品约束。允许破坏兼容，不代表目录换名、抽象数量或指标下降本身就是价值。

## 6. 建议实施顺序

### V2-0：校准事实与确定目标

- 对齐 AGENTS、规格、架构计划的现状与启动命令，保留历史决策的语境。
- 为公开 Python 导入、Hook、CLI、HTTP、配置和持久化行为列出“保留 / 修改 / 删除”清单。
- 将本报告的代码标准转为 V2 规格和逐批验收项，明确必要的破坏性变更与迁移收益。
- 用同一运行条件定位 unit 耗时波动；保持已有测试覆盖的语义。

出口：V2 的目标、边界、当前能力和未完成能力可以被清楚区分。

### V2-1：以组合根作为首个小批次

首选 `comet_rag/composition/bootstrap.py::build_context`（当前第 476 行起，203 行）。它同时承担资源配对、后端需求、模型和加载闸门、服务构造、PipelineConfig 合并。

先评估提取**配置合并**这一有明确输入输出的步骤，再评估资源选择和装配阶段。首次改动可以保持接口与行为，用较小风险验证新的写法；后续是否更换公开接口由具体设计收益决定。

| 项目 | 范围 |
|---|---|
| 影响范围 | `composition/bootstrap.py`、必要的同层辅助模块、`tests/unit/test_bootstrap.py`；调用方包括 CLI/API/worker 装配与注入测试 |
| 风险 | 中：配置显式值优先级、创建顺序、资源配对、共享闸门和清理顺序均有行为含义 |
| 必守行为 | 显式配置覆盖默认值；非法 revision 资源组合提前拒绝；模型共用预算；资源归属和关闭顺序明确 |
| 验证 | bootstrap/revision bootstrap/分层测试；unit、内存 E2E、Ruff、Pyright；独立阅读主装配路径 |

出口不是“函数少于某行数”，而是配置推导、资源选择和副作用顺序能被独立理解，且关键路径不因辅助层增加而更难阅读。

### V2-2：检索职责与可靠性闭环

两个方向拆成独立批次推进：

- `services/retrieval.py::_parents`（第 444 行起，77 行）适合审查身份校验、结果物化、批量读取和降级的职责。已有 `tests/unit/services/test_revision_retrieval.py` 覆盖排序、去重、rerank 粒度和父块回退；拆分风险为中，须保持这些顺序。
- 接续 M4 的生产入库激活编排、响应丢失恢复、superseded、GC 和生产接线，复用已有 DocumentStore 激活原语。涉及 `services/indexing.py`、`services/revision_context.py`、`services/ingestion.py`、DocumentStore、组合根及对应集成测试；风险为高，应按失败矩阵分批实施。

可靠性批次的重点是跨存储和任务边界：旧 active 在失败时仍可用、过期执行者不能提交、重试幂等、CPU→IO 交接有界、GC 不删除仍可见数据。真实 PG/Redis/Milvus 验收覆盖完成后才算闭环。

现有 `tasks/m4_t6_design.md:460` 已明确：写入首个 revision 向量以后，代码回退不等于数据回退。V2 必须复核该迁移与回滚顺序，而不是仅以单元测试通过宣布生产切换完成。

### V2-3：算法实现与发布验收

- `engines/documents/docx/parser.py::_get_paragraph_elements`（第 373 行起，131 行）是后续审查候选，风险高于组合根。按 OOXML 语义划分，使用快照和针对性断言验证；现有长度只是线索。
- `tasks/executor.py`、`tasks/store.py` 承载租约、CAS、取消和围栏，应围绕明确的职责问题和故障反例改动。保持核心不变式可见，比减少分支数更重要。
- 对实际性能路径比较前后结果；按现有口径完成 unit ≥70%、tasks 三层合并 ≥90%、core-only、对应真实后端与端到端验收。
- 发布说明列出导入、签名、配置、数据及运维变化，清理已完成迁移留下的过渡代码。

这些是审查候选与建议顺序，不是已经证明有缺陷的清单。首批应从 V2-0 的事实校准与 V2-1 的组合根小切片开始，用实测结果决定后续范围。
