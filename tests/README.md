# 测试说明

## 分层

| 目录 | 依赖 | 何时跑 |
|---|---|---|
| `unit/` | 无外部依赖 | **默认**。全套必须 < 10s，提交前必跑 |
| `integration/` | docker-compose 起中间件 | `-m integration` |
| `e2e/` | 全栈 | `-m e2e` |
| `benchmark/` | 全栈 | `-m benchmark` |
| `contracts/` | — | 不是用例，是**契约基类**，由上面各层的实现方继承 |

```bash
uv run pytest                      # 只跑 unit（addopts 已排除其余三类）
uv run pytest -m integration       # 需要 docker compose up -d
uv run pytest --cov=comet_rag --cov-report=term-missing
```

**硬性要求**：`unit/` 不允许打真实网络，也不允许依赖 GPU 服务。
需要 HTTP 时用 `httpx.MockTransport`（见 `unit/engines/test_url_loader.py`）。

## 查询 deadline 回归

共享预算、排队、超时降级和取消排干用例位于
`unit/services/test_query_deadline.py`，执行原语的迟到结果及清理守卫位于
`unit/services/test_query_deadline_execution.py`；真实应用配置到 HTTP 504 的接线位于
`e2e/test_ingest_search.py::test_configured_query_budget_covers_model_queue_and_returns_504`。
后者占满组合根创建的模型闸门，不依赖 GPU 或外部模型。规则见
[查询 deadline](../docs/query_deadline.md)。

## 入库线程生命周期

`unit/services/test_ingest_thread_lifecycle.py` 通过阻塞同步正规化、分块及文件清理，
验证一次/重复取消、取消后的线程异常、输入清理顺序和事件循环响应。真实
InProcessExecutor + Context 组合用例只通过 TaskStore 检查取消中/终态，不 gather
执行器内部协程，确保先排干线程再关闭 Loader/模型。测试无需网络、GPU 或真实中间件。

## Worker 夹具关停

worker 集成夹具先停轮询，取消 SDK 的业务 job_tasks，再等待 run_job 传输任务完成
Redis 结果确认，最后调用原生 Worker.close。不要取消正在确认结果的包装任务：
ARQ 0.28.0 的 close 会再次 gather 此表，CancelledError 会阻断后续连接池关闭。
`unit/test_worker_teardown.py` 使用真实 SDK close 与模拟池固定这个竞态；确认失败
仍传播，清理不能以吞掉异常的方式变绿。

## 契约测试

`contracts/` 下是与实现无关的测试基类。同一套测试要被多个实现跑：

```python
# tests/unit/tasks/test_store_contract.py
class TestInMemoryTaskStore(TaskStoreContract):
    @pytest.fixture
    async def store(self):
        return InMemoryTaskStore()


# 将来 tests/integration/test_store_postgres.py
class TestPostgresTaskStore(TaskStoreContract):
    pytestmark = pytest.mark.integration

    @pytest.fixture
    async def store(self, pg_dsn):
        return PostgresTaskStore(pg_dsn)
```

这是"换实现时行为不变"这句承诺唯一的兑现手段。

**执行器契约的纪律**：只允许通过 `TaskStore` 观察结果，绝不 gather 执行器
内部协程。`InProcessExecutor` 有 `_bg` 可等，而 `ArqExecutor` 的任务跑在
别的进程里根本没有本地协程 —— 只有把"跑完了"定义成"库里到终态了"，
两者才可能跑同一套测试。等待原语见 `contracts/support.py`。

## docx 快照测试

样本由 `fixtures/docx/build.py` **生成**，不提交 `.docx` 二进制：

- `.docx` 是 zip，进了 git 就是不可 review 的黑盒
- 仓库里现有的真实文档（`poc/docs/`）含实际项目内容，本项目计划开源，不应提交
- 生成脚本本身就是"样本里有什么"的可读说明

快照存放在 `fixtures/docx/snapshots/*.json`，内容是 `DocxParser.parse()` 的
完整输出（blocks + text）。

样式缓存另有两组定向测试：

```bash
uv run pytest tests/unit/engines/test_docx_style_resolution.py tests/unit/engines/test_docx_style_performance.py
```

前者以手写结果验证格式优先级、默认/缺失样式、Hyperlink、字段结果、表格及解析后
缓存释放；后者统计 python-docx 的默认样式扫描和继承链访问次数，确保重复 run
不会重复解析相同样式，不设依赖硬件的毫秒阈值。性能对照见
[性能基线](../docs/benchmark.md#docx-样式解析2026-10-22)。

### 更新快照

```bash
UPDATE_DOCX_SNAPSHOTS=1 uv run pytest tests/unit/engines/test_docx_parser.py
```

**先看 diff，再决定要不要更新。** 快照测试的全部价值在于"变了要有人看一眼"；
无脑重新生成等于把测试删掉，但还留着它带来的虚假安全感。

判断流程：

1. `git diff tests/fixtures/docx/snapshots/` 看清到底变了什么
2. 问自己：这是我这次改动**想要**的效果吗？
3. 是 → 更新快照，并在 commit message 里说明输出为何变化
4. 否 → 是回归，改代码不是改快照

新增样本时，在 `build.py` 的 `BUILDERS` 里加一项即可；首次运行会自动生成
快照并**故意失败**，提示你人工核对后再提交。

### 合成样本的局限

python-docx 产出的 XML 比 Word 真实输出简单得多，覆盖不到 Word 特有的
怪异结构（编号域、复杂嵌套、样式继承链）。`test_docx_parser.py` 里另有一条
可选用例：设置 `COMET_RAG_REAL_DOCS=1` 且本地存在 `poc/docs/*.docx` 时，会拿
真实文档跑**冒烟**（不崩、有产出），但不比对内容 —— 那些文档不进版本库，
无法维护稳定快照。显式开关避免开发者机器上恰好有私有样本时，默认单测耗时
突然增加且无法在 CI 复现。

## 编写测试的几条约定

**反向验证。** 新写的守卫/不变式测试，务必往实现里注入一次对应缺陷，确认它
真的会红。一个永远为真的测试比没有测试更糟 —— 它提供虚假的安全感。

**期望值不要从实现推导。** 例如状态机测试里的迁移矩阵是手写的
（`unit/tasks/test_states.py`）：写成 `can_transition(a,b) == (b in _ALLOWED[a])`
是拿实现验证实现，恒真、一无所证。

**注意测试数据是否真的走到目标代码路径。** 分块器测试最初用的是一串无空格
无换行的中文，导致所有"不变式"都从分隔符递归逻辑旁边绕过去、落到按字符
兜底的退化分支 —— 测试全绿，但测的不是真正会跑的那条路。覆盖率报告是
发现这类问题的主要手段。

**已知局限写成特征化测试，而不是回避。** 例如 `chunk_overlap` 在自然切分
单元较粗时会静默失效（`unit/engines/test_chunkers_variants.py`），
用测试把当前行为钉住，哪天变了（无论修好还是改坏）都会立刻被发现。

## 集成测试与中间件

```bash
docker compose up -d                  # postgres + redis + milvus(+etcd+minio)
docker compose up -d postgres redis   # 只要轻量的两个（几十 MB，秒起）
uv run pytest -m integration
```

**中间件没起时对应用例会 skip，不会 fail。** 集成测试是可选的：核心依赖环境
（CI 的 core-only job、只想跑单元测试的贡献者）根本没有 docker，让它们红一片
只会训练出"看到红色就忽略"的习惯，那比没有测试更糟。

端口刻意避开常用默认值：MinIO 映射到 **9010/9011** 而非 9000/9001 ——
后者在很多机器上已被占用，撞了之后的报错很难一眼看出原因。
可用 `COMET_TEST_POSTGRES_DSN` / `COMET_TEST_REDIS_URL` / `COMET_TEST_MILVUS_URI`
指向别的实例。

Milvus 集成测试固定使用 `zhihao_test_database`，不会回落到 `default`，也不会为测试
创建 database。首次运行前请让管理员预先创建该 database；缺失时用例会明确 skip 并
提示 database 名。`COMET_TEST_MILVUS_DATABASE` 即使设置成其他值也会直接拒绝运行，
避免测试误删非测试数据。

### 真实 MinerU

真实 PDF 测试没有默认地址。未设置地址或服务不可达时会明确 skip：

```bash
COMET_TEST_MINERU_URL=http://127.0.0.1:8989 \
  uv run pytest -m integration tests/integration/test_mineru_e2e.py \
  --mineru-report mineru-report.json
```

若已有 OpenAI 兼容的 MinerU 2.5 vLLM，先在 Comet-RAG 之外启动 MinerU 3.x
网关。当前 MinerU 使用环境变量配置远端模型；网关只监听本机时不需要开放
`server_url` 请求参数：

```bash
MINERU_VL_SERVER=http://<vlm-host>:<vlm-port> \
MINERU_VL_MODEL_NAME=opendatalab/MinerU2.5-2509-1.2B \
mineru-api --host 127.0.0.1 --port 8989

COMET_TEST_MINERU_URL=http://127.0.0.1:8989 \
COMET_TEST_MINERU_BACKEND=vlm-http-client \
uv run pytest -m integration tests/integration/test_mineru_e2e.py \
  --mineru-report mineru-report.json
```

先用 `GET /health` 确认响应含 `status=healthy` 与 `protocol_version=2`。vLLM 的
`/v1/models` 地址不能直接填给 `COMET_TEST_MINERU_URL`，因为它不提供 `/tasks`
协议。若将网关绑定到 `0.0.0.0`，MinerU 还要求显式确认其
`--allow-public-http-client` SSRF 风险；生产环境应配网络 ACL，不能把该开关当成鉴权。

若服务不是 `pipeline` 后端，可再设置 `COMET_TEST_MINERU_BACKEND`；解析方式和语言
分别由 `COMET_TEST_MINERU_PARSE_METHOD`、`COMET_TEST_MINERU_LANGUAGE` 覆盖。测试会
并发提交一个带文本层的 PDF 和一个纯图片扫描 PDF，通过 TaskStore 等待终态，并
记录耗时、Markdown 字节、Comet-RAG 进程的 Python 堆峰值及 CPU lane 预计持有
时间。报告不包含外部 MinerU 进程的 CPU、显存或 RSS，不能用来宣称服务总资源。

### 跑 arq 的用例时，Redis 键怎么隔离

worker 相关的集成测试要真的用 Redis，隔离方式分两种，选哪种取决于
**队列名本身是不是被测对象**：

| 用例 | 隔离方式 | 为什么 |
|---|---|---|
| `test_executor_arq.py`、`test_workers_split.py` | 每个用例一条随机队列名（db 0） | 队列名无所谓，随机最省事 |
| `test_e2e_workers.py` | 生产队列名 + **独立 redis db 15** | 它验的就是生产 `PROFILE`，队列名不能改 |
| `test_runner_registry_workers.py` | 生产队列名 + **专用 redis db 11/12/13** | 两套 Context 的消费与失败启动不能互相接管队列 |

第二种若也用 db 0，`_drain_queues` 会把开发机上真跑着的 `comet:cpu` /
`comet:io` 一起删掉 —— 测试删掉开发者的队列，属于最难联想到原因的那类事故。

worker 跑在测试进程自己的事件循环里（arq 的 `async_run()`）。调度全程走真实
Redis，少掉的只有 `fork`；而"生产端与消费端之间除了 Redis 与 TaskStore 再无
别的通道"这一点，靠**两个 executor 实例 + 两条独立数据库连接**来证明
（见 `test_executor_arq.py` 下半部分）。

每个 `Context` 与自己的执行器共用一份 `RunnerRegistry`。即使 API 和两个 worker
在同一测试进程运行，也分别使用自己装配的 runner；替换 loader 时必须逐个替换 worker
的绑定。执行器契约 fixture 显式注入 `tests.contracts.task_executor.RUNNERS`；新增独立
执行器时不能再依赖导入模块触发全局注册。

### RunnerRegistry 的真实 worker 验证

`test_runner_registry_workers.py` 使用真实 Redis、PostgreSQL 和生产 worker 装配，
仅注入不调用网络的模型与内存向量存储，专测任务执行与生命周期。场景包括六个
Context 的绑定隔离、两种关闭顺序、CPU 关闭后的 IO 续跑、IO 重试、移交投递失败
后补投，以及启动异常/取消后真实连接释放。任务结果通过 TaskStore 观察；失败清理
同时检查 PostgreSQL `pg_stat_activity` 与 Redis `CLIENT LIST`，并先证明连接确实存在。
这组测试已接入 `task-integration` CI，不需要 Milvus 或模型服务。

```bash
COMET_TEST_POSTGRES_DSN='postgresql+asyncpg://comet:comet@127.0.0.1:25432/comet_v2_test' \
COMET_TEST_REDIS_URL='redis://127.0.0.1:26379/0' \
COMET_TEST_REQUIRE_SERVICES=1 \
uv run pytest tests/integration/test_runner_registry_workers.py -m integration
```

测试前先迁移该独立数据库。生产队列所在的 11–15 DB 必须专供集成测试；它们的区分
是测试隔离手段，不代表共享任务表提供多租户路由隔离。`worker_config.py` 从探测通过的
端点构建 APPConfig，PostgreSQL/API 全栈与 worker E2E 不再写死 localhost 或开发库名。
当前 APPConfig 不能表达的 PostgreSQL 查询参数、Redis TLS/ACL 用户会明确拒绝，
不会转换为另一种连接方式。真实全栈/Milvus 链路仍由相应 E2E 独立验收。

`Worker.async_run()` 不释放 ARQ 自建连接池。持有自建 pool 的测试 worker 先停全部
轮询、排干全部 job，再调用原生 `Worker.close()`，由它触发生产 `on_shutdown` 并关闭
SDK 连接池；不能用仅关闭 Context 的替代函数。外部注入并共享的池仍由外部 fixture
在所有消费者和执行器停止后统一关闭。

### 清表一律走 `conftest.truncate_tables`

`TRUNCATE` 要 ACCESS EXCLUSIVE 锁；有别的连接还开着事务时，PostgreSQL 的默认
行为是**无限期等下去**。症状是整个 pytest 进程静默挂起 —— 没有输出、没有报错，
连卡在哪个用例上都看不出来（本项目真的挂过一次，11 分钟后人工掐掉才发现）。

所以清表统一走那一个 helper，它会先 `SET LOCAL lock_timeout`，拿不到锁就
`pytest.fail` 并指出"多半是上一个用例漏关了会话"。**新写的集成测试不要再自己
拼 TRUNCATE**：把"静默挂起"换成一条指名道姓的报错，是这个 helper 存在的全部理由。

**它后来真的抓到了一次。** 全套集成测试偶发地在 `test_store_postgres.py`
上一口气报 46 个 `LockNotAvailableError`，而单跑那个文件永远是绿的。
根因在**两个文件之外**：`test_executor_arq.py` 的夹具把执行器的 `shutdown()`
放在了"取消 worker 的 job 协程"**之前**。

job 被取消时，`execute()` 会 detach 一个 `_mark_cancelled` 协程去写库
（在被取消的协程里 await 不可靠，只能甩出去）。夹具紧接着关数据库，
那次写入于是连着一个正在拆的连接池 —— 连接被丢弃时事务既没提交也没回滚，
PostgreSQL 要等到发现套接字断了才收拾，**这段时间里它持有的锁还在**。

教训有两条：**异步测试的关停顺序是"先停生产者、再排干、最后关连接"**，
顺序错了不会当场报错，只会在别处偶发；以及，若没有那个 `lock_timeout`，
这件事至今仍表现为"整个测试进程静默挂起"，根本无从查起。

### 崩溃恢复为什么必须起真进程

`test_crash_recovery.py` 会 `subprocess` 起一个真的 arq worker，再对它
`SIGKILL`。**在测试进程里 cancel 一个协程不算崩溃** —— `execute()` 会接住
`CancelledError`、把任务干净地落成 CANCELLED，恰恰绕开了要验的那条路
（"worker 死得连一个字都没写下"）。

子进程从 `cwd` 读一份临时 config.yaml，入口是 `tests/integration/crash_worker.py`，
所以这条链路顺带也验了 `arq <module>.WorkerSettings` 这个 CLI 入口能起来。

## 数据库迁移

```bash
uv run alembic upgrade head            # DSN 从 config.yaml 读
uv run alembic -x dsn=postgresql+asyncpg://... upgrade head   # 显式指定
uv run alembic revision --autogenerate -m "描述"
```

`alembic.ini` 里的 `sqlalchemy.url` **刻意留空** —— 那个文件要进版本库，
写死密码迟早误提交。取值逻辑见 `alembic/env.py`。

新增 ORM 模型必须定义在 `comet_rag/infrastructure/database/models.py`
（或被它 import），否则 `--autogenerate` 收集不到，会把那张表判为"该删掉"。


## 真实 Milvus 与 revision 可见性

`.github/workflows/tests.yml` 的 `revision-integration` job 使用固定的官方
`milvusdb/milvus:v2.6.23`，按[官方 standalone 脚本](https://github.com/milvus-io/milvus/blob/v2.6.23/scripts/standalone_embed.sh)
使用内嵌 etcd、RocksMQ 和本地存储。配置在 `.github/milvus/`；端口是
29530/29091，数据卷为一次性匿名卷，不使用开发 compose 或生产数据。
这验收真实服务端的 Strong/BM25/revision 行为，不宣称覆盖远程对象存储故障或生产 HA。

CI 先创建 `zhihao_test_database`、迁移独立 PostgreSQL，再跑向量/文档仓储契约和
`test_milvus_async_lifecycle.py`、`test_revision_lifecycle.py`、
`test_revision_activation_recovery.py`、`test_revision_gc_recovery.py`。原生异步生命周期用例
验证同参数建索引幂等、服务端提交后响应丢失不会删掉另一实例的数据，以及部分索引
完成后取消仍能跨实例续建。revision 用例使用独立的 SQL/Milvus 读客户端，覆盖 legacy、
partial-write、ready、激活以及旧向量残留，在 dense/keyword/hybrid 的 child/parent
模式验证可见性；不靠 flush/sleep 掩盖跨客户端读空窗。

该 job 同时提供 Redis，验证 CPU→IO ready 移交：队列仅含 task_id，CPU worker 停止且
连接关闭后，IO worker 从 PostgreSQL context 和父块续跑；向量提交后响应丢失重用
同一 revision。测试 worker 运行于同一事件循环但连接独立，不能当作 fork/SIGKILL
验收。测试 kind 分别验收 prepare-to-ready 和独立 activation 阶段；激活提交后丢失响应
只重试 activation，不重做 indexing。该测试成功不代表生产 ingest 已接线。

激活恢复用例在 PostgreSQL 事务的状态/pointer flush 后、commit 前注入异常，验证真正
回滚；提交后再注入响应丢失，使用独立 PostgreSQL 与 Strong Milvus 读连接确认六种
检索组合只见旧/新 active。另覆盖旧 ready 任务与已激活后又被替换的任务重试，二者
都返回 superseded 而不能反杀新版本。此切片不执行 GC，也不模拟 fork/SIGKILL。

GC 用例在独立 PostgreSQL/Milvus 连接中覆盖 superseded、retired、部分删除、无异常的
残留向量、清父块事务回滚、删除/废弃/清理响应丢失、并发回收与误传 active 候选。
每个 fixture 在 GC 前先验证六种检索组合可见；清理后 active、legacy 和其他 source
向量必须保留，父块移除且 purged 墓碑仍存在。不用 sleep/flush 等待可见性。
共享 Store 契约验证废弃后不可再激活，以及候选列表只包含 retired/abandoned。
这验收独立 GC 切片，不代表生产 Task/maintenance 已接线或覆盖迟到写入。

第十七批补充 `unit/services/test_revision_publication.py` 和
`integration/test_revision_publication_recovery.py`：覆盖发布意图/结果写回前后失败、
abandon/purge/Task 确认响应丢失、旧 active 回执被新版本替换、清理阶段单独续跑、
身份/generation/预算校验，以及 abandon/delete/purge 后取消或租约丢失。
事件控制的故障边界延迟旧请求执行，真实 Milvus 客户端验证 GC 后仍能落库；独立
PostgreSQL Task/DocumentStore 与 Milvus 读连接仅凭保留意图补偿，不依赖原 Outcome。
六种检索组合仍只见新 active。该注入不模拟 Milvus 内部调度或真实进程 SIGKILL。
普通 sweep 不扫描 purged 的局限保留为特征化断言；未注册生产补偿扫描。

第十八批补充 `unit/services/test_revision_reconciliation.py` 与
`integration/test_revision_reconciliation_recovery.py`。共享 TaskStore 契约增加 11 项
参数化用例，覆盖同时间戳/Unicode 总序、锚点删除、插入新头部、分页校验及带版本删除
竞态；PostgreSQL 与内存必须通过同一套断言。应用组合根另验保留守卫，force 不能绕过。

持续对账覆盖页间新增、单项坏记录、预算、游标/作用域/整份 context 上限、确认提交
前后响应丢失、扫描者取消/租约丢失和并发激活保护。6 项真实 PG/Redis/Milvus 用例
从新 Context 或新 worker 尝试续页，重复清理 purged 后迟到向量，并核验六种检索组合。
故障注入定位于客户端提交/确认边界，不宣称模拟真实 SIGKILL 或服务端内部调度。

未传业务 guard 的底层删除会丢失恢复身份、offset 可饥饿，以及 prepare 写父块后
handoff 未保存的取消任务仍 deferred，均保留特征化测试；不把已知缺口写成完成承诺。
16 类单元/契约反向缺陷及 2 项真实后端反向实验均由目标断言检出；策略与生产门禁见
[持续对账设计](../tasks/v2_reconciliation_design.md)。

单元反向验证覆盖 context UTF-8 上限/CAS 冲突重验、模型返回后所有权围栏、完整计划
指纹及 UUID 重用；32 MiB 是整份 context 的持久化上限，不是进程峰值内存预算。

本地缺服务仍 skip；设置 `COMET_TEST_REQUIRE_SERVICES=1` 后，Milvus 缺依赖、
连接/鉴权失败或测试 database 不存在会 fail，不能用全 skip 的结果冒充验收。
CI 总耗时上限 20 分钟，服务健康等待上限 180 秒，并始终清理本项目测试容器/卷。

只验收真实 Milvus 时，可显式读取本地 `.env` 的 `MILVUS_URI`：

```bash
COMET_TEST_REQUIRE_SERVICES=1 uv run --env-file .env pytest \
  tests/integration/test_vectorstore_milvus.py \
  tests/integration/test_milvus_async_lifecycle.py -m integration
```

`COMET_TEST_MILVUS_URI` 优先于 `MILVUS_URI`；测试仍强制使用 `zhihao_test_database`，
不使用 `.env` 的应用数据库名。普通 `uv run pytest` 不会自动读取 `.env`；因此默认
localhost 的 skip 不能证明已配置的远程服务不可用。若 gRPC 被系统代理错误转发，
可为该测试主机设置 `no_grpc_proxy` / `no_proxy`，不必改全局代理设置。

每例只清理自己随机前缀下的集合。原生异步生命周期夹具会让清理错误直接导致失败，
避免把遗留半成品当成验收通过。索引失败后不自动删集合；续建只由显式
`ensure_collection` 触发，普通读写不会偷偷修复 schema。

## 覆盖率怎么量

spec S5 要求 `tests/unit` ≥ 70%、`comet_rag/tasks/` ≥ 90%。

```bash
uv run pytest --cov=comet_rag --cov-report=term          # unit：78%
```

但 `comet_rag/tasks/` 这条**不能只看 unit**：`ArqExecutor` 与
`PostgresTaskStore` 天生要中间件才跑得起来，只算 unit 的话它们分别是 33% 与
28%，看着像没测，实际是被 120 条集成用例盖着的。合并三层才是真实数字：

```bash
uv run coverage erase
uv run pytest -q --cov=comet_rag.tasks --cov-append
uv run pytest -q -m e2e --cov=comet_rag.tasks --cov-append
uv run pytest -q -m integration --cov=comet_rag.tasks --cov-append
uv run coverage report --include="*/comet_rag/tasks/*"     # 96%
```

把"要中间件才能覆盖的代码"算成未覆盖，会推着人去写 mock 掉一切的假单测 ——
那种测试通过率很好看，但换个后端立刻失效。契约测试的存在正是为了不走那条路。
