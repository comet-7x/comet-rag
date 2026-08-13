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
可选用例：本地存在 `poc/docs/*.docx` 时会拿真实文档跑**冒烟**（不崩、有产出），
但不比对内容 —— 那些文档不进版本库，无法维护稳定快照。

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

### 跑 arq 的用例时，Redis 键怎么隔离

worker 相关的集成测试要真的用 Redis，隔离方式分两种，选哪种取决于
**队列名本身是不是被测对象**：

| 用例 | 隔离方式 | 为什么 |
|---|---|---|
| `test_executor_arq.py`、`test_workers_split.py` | 每个用例一条随机队列名（db 0） | 队列名无所谓，随机最省事 |
| `test_e2e_workers.py` | 生产队列名 + **独立 redis db 15** | 它验的就是生产 `PROFILE`，队列名不能改 |

第二种若也用 db 0，`_drain_queues` 会把开发机上真跑着的 `comet:cpu` /
`comet:io` 一起删掉 —— 测试删掉开发者的队列，属于最难联想到原因的那类事故。

worker 跑在测试进程自己的事件循环里（arq 的 `async_run()`）。调度全程走真实
Redis，少掉的只有 `fork`；而"生产端与消费端之间除了 Redis 与 TaskStore 再无
别的通道"这一点，靠**两个 executor 实例 + 两条独立数据库连接**来证明
（见 `test_executor_arq.py` 下半部分）。

⚠️ **runner 注册表是进程级全局的**。测试把三个 `Context`（API + 两个 worker）
挤进同一个进程，最后一次 `wire_runners` 说了算 —— 真实部署下各进程各有一份，
不存在这个问题。`test_e2e_workers.py` 里有注释标出了这处差异。

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
