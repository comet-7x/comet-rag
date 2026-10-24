# 开发规范

Comet-RAG 同时交付库和参考服务。修改前先读 [规格与边界](tasks/spec.md)、
[架构](docs/architecture.md)；当前工作顺序见 [V2 后续计划](tasks/v2_next_steps.md)。
配置字段和工具规则以 `pyproject.toml`、`config.example.yaml` 与源码为准。

## 职责与依赖

- `composition/` 创建并装配模型、客户端和资源；API/worker 从 Context 取依赖。
- `services/` 编排用例，不导入具体 infrastructure；外部协议留在适配器中。
- `engines/` 只导入本项目 engines/ports 与允许的纯计算依赖，不认识队列、数据库或 HTTP 路由。
- `ports/` 只依赖标准库与自身，描述可替换实现共同遵守的契约和值对象。
- `core/` 不反向依赖本项目其他包。层级和包间无环由
  [AST 守卫](tests/unit/test_layering.py) 检查。

增加 engine 第三方依赖、Task 持久化字段、向量 schema/metadata 字段、核心 Task/Vector
抽象接口、新中间件或第三个向量后端前，按 spec §7 先确认设计。当前后端范围仍是
Milvus + InMemory；研究报告不等于已批准接入。

## 可读性

1. 先明确函数的输入、结果与失败语义，再选择类或抽象。类应有清楚的状态或生命周期职责。
2. 用显式控制流表达阶段、事务和资源所有权。不要为了少写几行制造多层转发、通用注册器
   或可配置工作流；只有实际复用或变化点足以解释成本时才提取。
3. 不以文件长度、函数行数或类数量作为重构验收标准。保留一个可顺着读完的业务流程，
   比把它拆成大量只调用下一个函数的方法更重要。
4. 使用 `from __future__ import annotations`；公开 API 全量类型标注，`Any` 邻近说明原因。
   跨层契约优先 Protocol；需要模板方法等共享实现时再使用 ABC。
5. 引擎内部数据、Task 使用 `dataclass(slots=True)`；配置与 HTTP DTO 使用 Pydantic。
6. 注释说明契约、边界和反直觉原因，不复述代码。中文注释保持现有风格；具体标准只维护在
   [comment_style.md](docs/comment_style.md)，不为每个函数补模板段落。

沿用现有 Ruff 和 Pyright 配置，不从外部 skill 整段覆盖行宽、类型检查等级或依赖。

## 同步、异步与资源

- 双模式入口使用 `Foo` / `AsyncFoo`，方法同名，不保留旧 `a*` 别名；纯计算和值对象共享。
- 先核查目标版本上游 API，优先原生异步。Rust/C 扩展、释放 GIL 和 async 是不同问题，
  必须分别验证；同步阻塞工作使用既有的有界执行路径。
- 需要线程桥时复用 `engines.offload.run_sync`，取消后等待工作结束再释放输入与资源。
- 客户端应用级复用。关停先停生产者、再排干、最后关闭连接；保持已有进程级并发预算。
- 不用空列表、`None` 或泛化捕获掩盖存储失败。重排、父块读取等既定降级要记录诊断；
  取消、所有权丢失和不可降级错误保持各自语义。
- revision 激活与 GC 不共享一个“失败就回滚”的处理器：响应丢失不证明事务未提交。
  active 保护、墓碑与精确过滤见 [架构](docs/architecture.md)。

## 测试与提交

```bash
make install
uv run pytest
uv run pytest -m e2e
make lint
```

按改动范围运行 integration/benchmark；中间件准备、隔离库、清表和覆盖率合并统一见
[测试说明](tests/README.md)。unit 不打真实网络或依赖 GPU，目标仍是小于 10 秒；
冷跑超标应记录、定位，不能删除测试、改选择或放宽门槛来掩盖。

新增实现继承共享契约。故障测试要真正进入目标分支，期望独立书写；新增守卫应反向
注入对应缺陷，确认断言会失败。测试样本或结果没有跑过时，不写成“验证通过”。
DOCX 快照先看 diff 再决定更新；不得直接刷新以消除失败。

按可独立审查的职责提交代码、测试与文档，使用约定式提交，如 `fix(milvus): ...`、
`feat(indexing): ...`。分支遵循仓库现有前缀规则。生产开关、迁移和 API 变化必须说明
兼容与回滚边界；PR 创建、推送和发布按本次任务授权执行。

## 文档只有一个事实来源

| 信息 | 维护位置 |
|---|---|
| 产品范围、需确认的边界 | [tasks/spec.md](tasks/spec.md) |
| 下一批工作与验收条件 | [tasks/v2_next_steps.md](tasks/v2_next_steps.md) |
| 历史实现和验证记录 | [tasks/v2_plan.md](tasks/v2_plan.md) |
| 模块边界与调用流程 | [架构](docs/architecture.md)、[结构与流程图](docs/structure.md) |
| 库使用、部署配置、注释标准 | 各自已有使用文档；README 只提供入口 |

同一项修改同步更新相应图与说明。图上的“已实现”“待接线”“默认关闭”必须能从代码
和配置核对；外部框架宣传、研究候选和目标架构不能画成现行能力。
