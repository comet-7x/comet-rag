# 架构

## 一句话

**一个通用的 RAG 框架，同时是库和参考服务。** 库那一半（`engines/`）只依赖
pydantic/httpx/lxml 一类的纯计算包；服务那一半在它之上加了任务调度、持久化
与 HTTP 接口。两半共用同一份解析与切分代码。

## 分层

```
    composition/    组合根：把配置装配成一整套资源  ┐
    api/            HTTP 入口（FastAPI）          ├─ 参考服务
    workers/        消费端进程（arq）             ┘
        ↓
    services/       用例编排（入库、检索、知识库）
    schemas/        HTTP 请求/响应 DTO
        ↓
    tasks/          通用任务框架（与 RAG 无关）
    infrastructure/ 向量库、供应商客户端、数据库
        ↓
    engines/        纯计算：加载、解析、清洗、切分、排程   ← 「库」就是这一层
        ↓
    ports/          契约（Protocol）与其词汇表（值对象）   ┐
    core/           闸门、降级、日志、时间 —— 人人依赖       ├─ 零依赖地基
    config/  exceptions/                                    ┘
```

依赖方向**只能向下**。`tests/unit/test_layering.py` 用 AST 强制执行五条：

1. `engines/` 不得 import 任何基础设施包（redis / pymilvus / sqlalchemy /
   arq / fastapi …）。破了这条，用户为了跑一个 docx 解析器就得装一整套中间件，
   "库"这一半当场作废（spec A1）。
2. `engines/` 只能 import `engines` 与 `ports`（**白名单**）。
3. `services/` 与 `engines/` 不得直接 import `infrastructure.providers` ——
   具体供应商只在组合根选择。
4. `core/` 不得 import 本项目任何其他包。它是人人依赖的零依赖内核，
   一旦回头依赖上层就出环。
5. 顶层包之间不得成环。环里的包无法被单独理解或单独拿走。

第 2 条原本是一份黑名单（禁止 api / workers / services）。黑名单只拦得住
已经想到的那几个包：后来新增的 `application/` 就从缝里溜了进去，`pipeline.py`
运行时 import 了它，而本文档白纸黑字写着 engines 在最底层。**文档说的规则
和守卫执行的规则不是同一条，中间差出的那个洞刚好够放一个错误。** 改成白名单
之后没有这个失效模式：新包默认被拒。

还有第六条守卫：单进程模式下的任何模块都不得 import `workers.maintenance`
（租约回收），理由见下文「崩溃恢复」。

### 为什么 `ports/` 在最底层

契约必须比所有使用者都低，否则使用者就得向上依赖。`engines/` 需要说出
"我要一个 embedding 模型"；如果契约住在某个上层包里，engines 就只能反向依赖 ——
这正是上面第 2 条要防的事。`ports/` 不 import 本项目任何其他包，所以谁依赖
它都是向下。

值对象（`MediaResource`、`RerankDocument` …）也放在 `ports/`：它们是 Port
签名里出现的类型，也就是这套契约的词汇表。

## 三个核心抽象

每一个都配有一套**与实现无关的契约测试**（`tests/contracts/`）。
换实现时跑同一套契约，这是"换后端行为不变"这句承诺的唯一兑现手段 ——
不是靠文档，是靠 79 条断言。

| 抽象 | 内存实现 | 真实实现 | 契约条数 |
|---|---|---|---|
| `TaskStore` | `InMemoryTaskStore` | `PostgresTaskStore` | 37 |
| `TaskExecutor` | `InProcessExecutor` | `ArqExecutor` | 15 |
| `BaseVectorStore` | `InMemoryVectorStore` | `MilvusStore` | 27 |

`KnowledgeBaseRepository` 另有 10 条。

### 为什么 store 与 executor 是分开的

`TaskStore` 回答"这个任务现在怎么样了"（持久、可查询）；
`TaskExecutor` 回答"接下来谁该干活"（短暂、消费即消失）。

合成一个的话，要么队列变得不可查询，要么数据库被当成高频争抢的队列用。
更直接的理由：`spawn(task, coro)` 收的是协程对象，Redis 里没地方放协程 ——
一旦把它写进"换实现时签名不变"的接口，这句承诺就是假的。

拆开之后，**队列里只放 `task_id`**，任务数据一律回库读。于是「重试」
「崩溃恢复」「断点续跑」「跨 worker 移交」全都退化成同一个动作 `submit(task_id)`。

## 任务的一生

```
PENDING ──► RUNNING ──┬─► SUCCEEDED
   ▲         │        ├─► FAILED ──► PENDING（人工 retry）
   │         │        └─► CANCELLING ──► CANCELLED
   └─────────┴ 可重排队（可重试失败 / 租约回收 / 换道移交）
```

状态只能经 `TaskStore.transition()` 改，守卫是一张手写的迁移表
（`tasks/states.py`）。乐观锁 CAS 保证并发写不丢更新；
调用方**没传** `expected_version` 时冲突会自动重读重来，
传了才把冲突抛给它 —— 这个区分是跨进程部署逼出来的（见 `store.py::_cas`）。

## 两种部署形态

### 单进程（开发、小规模）

```
comet-rag serve      # API + InProcessExecutor，任务在本进程的协程里跑
```

不需要 Redis，不需要 worker。分道信息被忽略，整条流水线一口气跑完。

### 跨进程（生产）

```
comet-rag serve                 # 只投递，不执行
comet-rag worker preprocessor   # CPU 密集：解析 / 清洗 / 分块
comet-rag worker embedder       # IO 密集：向量化 + 写库
```

**worker 按负载特征分，不按业务名词分。** 判据只有一条：这类活该怎么扩容。

| | 负载 | 扩容方式 | 默认并发 |
|---|---|---|---|
| preprocessor | CPU 密集（GIL 挡着） | **加进程**（副本 ≈ 核数） | 2 |
| embedder | IO 密集（等回包） | **加并发**（调 max_jobs） | 32 |

反过来会真的更慢：给 embedder 加进程会把对模型服务的连接数乘以进程数，
请求在服务端排队，而每个进程只看到"上游变慢了"，看不出是自己造成的。

流水线在 `chunking` 与 `indexing` 之间**移交**：任务退回队列、带上
`resume_stage`、投到另一条道。移交复用的正是断点续跑那套机制 ——
两者在实现上是同一件事，区别只在原因。

## 崩溃恢复

worker 被 `kill -9` 时，库里那条任务会永远停在 RUNNING：没人推进、没人报错。
所以跨进程部署必须挂租约回收（`workers/maintenance.py`）：心跳超过 lease
（90s，是心跳间隔的 9 倍）就判定 worker 已死，退回队列。

**单进程模式绝不能挂** —— 那时协程还活在本进程里，回收会造出"一份任务两个
执行者"。保证方式不是开关（会被配错），而是结构：`sweep_cron` 只在 `workers/`
下注册，单进程部署根本不加载那个包，且有 AST 守卫盯着。

租约判死做不到准确（"没心跳"和"死了"分不清），所以执行器侧还有一道**围栏**：
写终态前确认任务还归自己管，被接管了就一个字都不写。
**lease 负责少误判，围栏负责误判了也不出错**，两者缺一不可。

## 资源治理

```
                        ┌── 闸门（进程级，embedding 与 rerank 共用）
POST /ingest ─► 积压上限 ─┤
                        └── 降级：关 rerank → 降 top_k → 拒新任务
```

**闸门必须是进程级的。** 曾经是"每次调用建一个信号量"，于是 32 个任务各开
4 路扇出 = 对模型服务 128 路并发，而配置写的是 4，监控上完全看不出来。

闸门做在适配器基类的模板方法里（`aembed` 不可覆写，子类只能实现 `_aembed`），
所以"绕过闸门"在结构上做不到 —— 包装器只是约定，会被绕过且不报错。

`services/` 只依赖 Port 和共享值对象，不再直接依赖 `infrastructure.providers`。
具体的 Qwen/OpenAI 类只在组合根中选择并装配；这让模型实现可以替换，而查询、
入库和重排用例不需要认识供应商请求字段。

所有队列都有界：闸门外的等待席位、等待时长、待执行任务数，任一越界都
**明确拒绝**（HTTP 429），不静默排队。

## 相关文档

- [目录结构与核心流程](structure.md) —— 东西放在哪、一次请求怎么走完（含依赖图与流程图）
- [部署](deployment.md) —— 两条路径的具体命令与配置
- [性能基线](benchmark.md) —— 数字的定位与采集方式
- [Pipeline 用法](pipeline_usage.md) —— 只当库用时看这个
- [docx 解析内幕](docx_parser_internals.md)
- [MinerU 集成](mineru_integration.md) —— M2
