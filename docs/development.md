# V2 开发环境

Python >=3.12；开发依赖通过 `uv sync --locked` 安装到当前 checkout 的 `.venv`。默认测试不访问网络、模型或数据库。`make check` 运行静态检查、单元测试和构建，`make coverage` 单独显示当前实现的覆盖率。

## 独立后端配置

根目录 `docker-compose.yml` 使用项目名 `comet-rag-v2`，不指定固定容器名。持久数据使用该项目的命名卷，不挂载旧版 `.docker-data`。所有宿主端口仅监听回环地址：

| 服务 | 默认宿主端口 | 可覆盖的环境变量 |
|---|---|---|
| PostgreSQL | 15432 | `COMET_V2_POSTGRES_PORT` |
| Milvus | 29530 | `COMET_V2_MILVUS_PORT` |
| Milvus 健康检查 | 29091 | `COMET_V2_MILVUS_HEALTH_PORT` |

etcd 只供 Milvus 内部使用，不发布宿主端口。镜像为 PostgreSQL 17、Milvus 2.6.23 和 etcd 3.5.16。Milvus 使用其官方 Standalone 支持的本地存储和 Woodpecker 本地 WAL，均写入独立命名卷，不依赖 MinIO 镜像。该配置适合单机开发，不提供跨节点存储保证。配置依据为 [v2.6.23 官方启动脚本](https://github.com/milvus-io/milvus/blob/v2.6.23/scripts/standalone_embed.sh)和 [Woodpecker 2.6 配置](https://milvus.io/docs/v2.6.x/use-woodpecker.md)。

```bash
docker compose config --quiet
make db-up
make db-down
```

启动由开发者显式执行。PostgreSQL 开启默认持久化，默认开发账号为 `comet_v2`，数据库为 `comet_rag_v2`，默认密码为 `comet_v2_dev`，可通过 `COMET_V2_POSTGRES_PASSWORD` 修改。这些是本地开发配置。

阶段 0 仅检查 Compose 结构；后续验证使用独立实例。PostgreSQL 适配器显式调用 `prepare()` 创建默认 `comet_docstore_v2` schema/tables（清单格式 v2，JSON 保留实际数值表示），旧格式在新 scope 重建，Milvus 按 scope 创建独立集合。应用注入的 engine/client 由创建者关闭；源码清理不操作已有数据。现存卷会保留初始化凭据，修改 Compose 环境变量不会自动改变旧卷内账号；不要为了测试修改既有账号或删除数据卷。

## 包验证

```bash
uv build
comet_project_dir="$PWD"
comet_core_check_dir="$(mktemp -d)"
uv venv "$comet_core_check_dir/venv" --python 3.12
uv pip install --python "$comet_core_check_dir/venv/bin/python" dist/comet_rag-0.2.0.dev0-py3-none-any.whl
cd "$comet_core_check_dir"
"$comet_core_check_dir/venv/bin/python" -I "$comet_project_dir/scripts/check_core_install.py" --output-dir "$comet_core_check_dir/roundtrip" --phase write
"$comet_core_check_dir/venv/bin/python" -I "$comet_project_dir/scripts/check_core_install.py" --output-dir "$comet_core_check_dir/roundtrip" --phase read
```

选择尚不存在的验证环境目录。CI 在独立临时目录安装 wheel，通过 `scripts/check_core_install.py` 的 write/read 两个进程验证真实文件解析、codec、核心索引和 keyword 检索，不依赖仓库当前路径或开发环境。构建白名单包括源码、当前文档/测试、examples、evals 和验证脚本；本地配置、模型、资料与清理备份不进入分发包。

本地模型额外安装：`uv sync --locked --extra local-models`。普通 `uv run --locked` 可把未选择的 optional extras 从环境移除；已经安装模型依赖并做实际验证时，可直接使用 `.venv/bin/python`，或明确带相同 `--extra local-models`。不要用其它 checkout 的 VIRTUAL_ENV 推断当前 worktree 可用。

严格集成环境变量见 [测试说明](../tests/README.md)，实际持久文本与图片调用见 [examples](../examples/README.md)。服务 healthz/Loaded 不是业务可读保证，适配器会用配置超时内的实际查询检查 Milvus 数据分片就绪。重启只针对调用方明确拥有的测试工作负载，不把客户端重连视为容器重启。
