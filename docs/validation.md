# V2 验证记录

核验日期：2026-10-29。所有源码修改与工具运行针对 `refactor/comet-rag-v2` 当前 worktree；没有推送，没有迁移旧数据库或删除既有卷、集合、资料和私有配置。阶段 0/A 的历史证据保留，当前实现不能仅凭那些检查算通过。

## 当前出口

| 阶段 | 实际状态 |
|---|---|
| 0 / A | 已完成并提交，见 [0](phase0.md) / [A](phase-a.md) |
| B | 本地 TXT/DOCX→真实模型→PostgreSQL/Milvus→三模式及实际服务重启读回通过；最终身份修订后新 scope 重建/新进程回读通过，见 [B](phase-b.md) |
| C | 本地/HTTP/原生 DOCX 已验证；S3 与 MinerU 真实服务未验收，阶段保持开放，见 [C](phase-c.md) |
| D | HTTP/文档图片、四种真实组合和新进程持久回读通过；本轮图片范围服务重启待完成，见 [D](phase-d.md) |
| E | 父子/重排完整性、实际模型质量与成本比较通过，默认策略未启用，见 [E](phase-e.md) |
| F | 导出、示例/评估、构建白名单、架构守卫与 CI 已实现；最终完整验收依赖 C/D 的剩余出口 |

## 可复验证据

- PostgreSQL 五项真实集成通过，最终一次为 0.45 秒。包含真正事务回滚故障、持久快照/实际表示/清单、范围隔离、确认并发、损坏拒绝和明确删除。
- Milvus 两项 + 实际 HTTP 两项组合通过，64.21 秒，另有一个 S3 用例显式未选中。向量测试核对明确 IDs/指纹，过滤在 limit 前执行；没有拿 count 或健康状态代替内容。
- 文本 `phase-b-24f96d492310`：四文档实际 CLIP 512 维入库，dense/keyword/hybrid 均命中预期，最新身份规则下新进程复验通过。
- 图片 `phase-d-24f96d492310`：在表示资产守卫加强后的最终代码上，五文档实际入库，猫/狗分别四种模态共八次检索，HTTP 临时下载释放，DOCX 图像可检索，新进程逐 ID/资产 hash 复验通过。
- 服务真正重启记录：`phase-b-d4d059fb5dbe` 的 PostgreSQL/Milvus 容器重启后新进程核对全部内容与三模式通过。该次早于最终表示身份修订；后续范围不冒用这个重启结论。
- 真实 CLIP/MiniLM 评估在 `tmp/eval-phase-e-final.json`，8 查询/12 文档及两个图片，完整输入/标签/分数/时间/文件指纹；有限结果见 [evals](../evals/README.md)。不是生产基准。
- 核心 wheel 在仓库外新建环境，仅 comet-rag + lxml 6.1.3；`python -I` 两个独立进程实际解析文件、持久 JSON、索引重建与 keyword 检索，得到相同 document/unit IDs，未加载可选 SDK。wheel/sdist 内容检查排除 tmp、模型、凭据与清理备份，并包含 examples/evals/scripts。

最终开发检查通过：`make install`、`make check`、`make coverage`，606 项单元测试通过，Ruff 检查/格式通过，Pyright 零错误，wheel/sdist 构建成功。单元覆盖率为 90%（4536 条语句，470 条未覆盖），包含未在 unit 中连接真实数据库的 PostgreSQL 适配器；不按覆盖率或测试数量推导功能验收。

提交钩子对全部已跟踪及新增交付文件通过，包括 Ruff、Bandit、detect-secrets 与全部单元测试。仅逐行标注固定 Python 子进程、公开模型 revision 和无效凭据 URL 反例的已核对误报；未扩大扫描忽略范围。无适用文件的 symlink/JSON/YAML 排序钩子正常不执行。Compose 配置和 diff 空白检查通过；远端 GitHub Actions 未运行。

最终核心安装环境位于本次任务临时目录 `core-install-eiitfj0a`，仅包含 comet-rag 0.2.0.dev0 与 lxml 6.1.3。两个独立进程同时核对文档/索引身份、原始空白、Markdown 派生结果和实际 keyword 检索。

## 实际版本

Python 3.12.13；PostgreSQL 17.11；Milvus 2.6.23；etcd 3.5.16；SDK/工具：

| 包 | 当前验证版本 |
|---|---|
| torch | 2.14.1 |
| transformers | 4.57.6 |
| Pillow | 13.0.0 |
| httpx | 0.28.1 |
| SQLAlchemy | 2.1.6 |
| asyncpg | 0.32.0 |
| pymilvus | 2.6.17 |
| lxml（开发） | 6.1.0 |
| boto3 | 1.43.120 |
| pytest / Pyright / Ruff | 9.0.2 / 1.1.411 / 0.15.9 |

模型 revision、权重和图片 SHA 固定在 [examples](../examples/README.md)。本地文件 SHA 可以证明本轮实际使用的字节；不是远端部署永不变化的承诺。核心外部环境 lxml 6.1.3 与开发锁定版本不同，均实际检查。

## 功能提交

本轮按职责提交，未推送，commit 不含 AI 署名：

- `0ca651f`：受限来源获取、原生 TXT/DOCX 与 Markdown 派生导出。
- `b9d4c53`：范围切分、实际单元/表示、严格 codec 与嵌入模型。
- `ea00470`：PostgreSQL 内容事务/清单及 Milvus dense/BM25。
- `0872b6c`：持久图片资源与明确 partial 的 MinerU 协议；未将未验收服务标为通过。
- `ff74e18`：入库/搜索、精确删除、单元归并、父上下文与可选重排。
- `35a9f01`：可运行的持久文本/图片示例与真实模型策略评估。

构建/CI、核心安装脚本、架构守卫和本文随最终交付提交。阶段 B 与 E 的功能及验证已交付；C/D/F 的剩余真实出口如下。

## 尚需解除的阻塞

1. S3：自动审批拒绝运行选定的 SeaweedFS 镜像，理由 `[Code from External]`。没有启动或绕过，没有 AWS/兼容 S3 真实验收。
2. MinerU：缺少明确可用的 2.6.8 `/file_parse` 部署。MockTransport 与官方源码只能证明适配代码；不能证明识别质量或服务响应。
3. 图片服务重启：自动审批拒绝重启本次专用 Milvus 测试容器，理由 `[Interfere With Workloads]`。已展示本会话创建该容器/独立卷的成功记录，仍被拒；后续脚本移除重启，只运行授权范围内的新进程重连。

没有读取私有配置/凭据来解除这些阻塞，没有通过其它 agent 执行被拒动作。严格集成入口和环境变量见 [tests](../tests/README.md)。未完成项不 skip 为绿、不按已完成阶段报告。
