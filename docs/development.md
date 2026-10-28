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

阶段 0 只检查 Compose 结构，不创建容器、不初始化业务表、不运行数据库迁移。新版 DocStore 后续显式创建自己的 schema，Milvus 后续按 scope 创建独立集合；源码清理不操作已有数据。

## 包验证

```bash
uv build
uv venv /tmp/comet-rag-core --python 3.12
uv pip install --python /tmp/comet-rag-core/bin/python dist/comet_rag-0.2.0.dev0-py3-none-any.whl
/tmp/comet-rag-core/bin/python -I -c 'from comet_rag.retrieval import reciprocal_rank_fusion; assert reciprocal_rank_fusion({"dense": ["a"]})[0].unit_id == "a"'
```

选择尚不存在的验证环境目录。CI 在独立临时目录安装 wheel 并导入核心模块，验证不依赖仓库当前路径或开发环境。构建白名单只包括源码、当前文档和测试等项目文件，本地配置、资料与清理备份不进入分发包。
