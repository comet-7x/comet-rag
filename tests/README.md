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
