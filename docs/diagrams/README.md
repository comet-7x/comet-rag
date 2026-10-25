# 交互架构图

[打开 Comet-RAG 交互架构图](comet-rag.html)。HTML 可直接用浏览器离线打开，支持节点
聚焦、缩放、深浅主题和导出；无需启动项目或安装前端依赖。

图聚焦库入口、当前入库主链路、可选维护与离线预检，不穷举 import、部署副本或检索调用。
维护默认关闭，页进度和错误可查询；准备意图覆盖移交前取消。默认 legacy 保持原路径。
第二十一批增加独立 RevisionIngestRunner 节点，标出启用条件和对 Task/Document/VectorStore
的实际 IO，默认不打开新写入或定时补偿；接线能力不代表用户资料已迁移或生产已切流。
第二十批标出 API 准入可暂停及缺载荷保留；暂停不停止已受理任务，也不证明远端排干。
第二十二批增加独立 revision-preflight CLI 节点：只读四方配置，不向 Store 画 IO 箭头；
卡片区说明隔离重建与切读/增量回退演练，不表示项目含自动流量控制器或生产已切换。
历史载荷处置、新命名空间重建和生产切换门禁见 [迁移边界](../revision_migration.md)。
详细恢复流程仍见 [架构](../architecture.md) 和 [对账设计](../../tasks/v2_reconciliation_design.md)。

## 源数据与证据

- [comet-rag.architecture.json](comet-rag.architecture.json) 是可编辑的图源；HTML 是生成产物，不手改。
- 本图源码证据固定到 `f76f7b450b65c4cbeef219b62038d7347cb4d4a6`，各节点列出相对路径与行号。
  固定的是被说明的代码提交，不要求等于包含生成图的提交，避免自引用。
- 当前为 `local-only` 源码模式：保留路径、行号和版本，不假设尚未推送的提交可在 GitHub 打开。
- 使用 [Archify 3.2.0](https://github.com/tt-a1i/archify/tree/25cf37080fc8b1dbe758a4efc3935b55f3327083)，
  工具提交 `25cf37080fc8b1dbe758a4efc3935b55f3327083`。生成器不纳入项目依赖。
  查看器的 MIT 许可保存在 [ARCHIFY-LICENSE.txt](ARCHIFY-LICENSE.txt)，字体许可内嵌于 HTML。

## 随代码更新

影响图中职责、调用或“已实现/待接线”状态的代码修改，在同一批变更中同步更新此图：

1. 先提交并验证代码，读取该提交的调用方、条件分支与实际 IO；不要把工作区内容冒充旧提交证据。
2. 修改 JSON 的节点、连接、旁注和源码行号，将 `meta.repository.revision` 固定到该代码提交。
   HTML 输出路径保持 `docs/diagrams/comet-rag.html`，不积累按日期命名的副本。
3. 在仓库根目录使用外部工具重新生成，并执行源码、产物和真实浏览器检查：

```bash
ARCHIFY_ROOT=/path/to/archify
ARCHIFY_REVIEW_DIR=$(mktemp -d /tmp/comet-rag-archify.XXXXXX)
node "$ARCHIFY_ROOT/archify/bin/archify.mjs" finalize architecture \
  docs/diagrams/comet-rag.architecture.json docs/diagrams/comet-rag.html \
  --repo-root . --quality showcase --out-dir "$ARCHIFY_REVIEW_DIR" --json
```

4. 检查节点证据、浏览器报告和图中文字；布局或文字变化时查看实际页面。同步此处的代码/
   工具版本，并将 JSON、HTML、相关说明一起提交。检查失败不能写成已验证。

检查回执与截图留在临时目录；工具生成的同目录 `*.delivery.json` 只用于本地溯源，已忽略。
不提交截图、工具源码、node_modules 或机器绝对路径清单。本轮源码/产物/浏览器检查通过，
生产 revision 写入已提供默认关闭的接线；检查通过不代表用户数据已迁移、生产已切流，
或运维已实际开启对账/采集告警。

HTML 标记为生成文件，保留上游输出字节以匹配产物校验；仅该文件豁免空白检查。
JSON 与 Markdown 继续接受正常检查，不为清理模板里的空行而手改已验证的 HTML。
