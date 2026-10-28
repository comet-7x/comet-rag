# 交互架构图

[打开 Comet-RAG 交互架构图](comet-rag.html)，或查看[MinerU V1许可与证据读取细节](mineru-v1.html)。HTML 可直接用浏览器离线打开，支持节点
聚焦、缩放、深浅主题和导出；无需启动项目或安装前端依赖。

图聚焦库入口、默认legacy与可选revision主链路、独立维护、检索和仓库评测工具，不穷举
import、部署副本或所有检索连接。箭头表示调用/数据路径；评测的内存索引独立于生产Store。
队列只传task_id，CPU/IO经Store续跑；可选revision写入/判活/对账必须成套启用，默认关闭。
图中的cron只画可选revision对账，通用租约回收仍在ARQ worker默认注册。

旁注按职责说明当前不变式、PDF页事实、质量边界和发布门禁，不继续堆叠逐批历史。
第三十五批已完成本机中转的真实MinIO/PDF/模型/Milvus链路及重启恢复；不代表用户正式
部署或资料迁移完成。原始用户资料Git忽略，只在仓库保存必要数字与结论。

- [本轮收口和发布门禁](../evaluations/closeout_20261026.md)
- [原生PDF与表格质量探索](../evaluations/native_quality_20261026.md)
- [全量中间件与预算口径](../evaluations/validation_20261026.md)
- [迁移边界](../revision_migration.md)、[架构](../architecture.md)、[对账设计](../../tasks/v2_reconciliation_design.md)

## 源数据与证据

- [comet-rag.architecture.json](comet-rag.architecture.json) 是可编辑的图源；HTML 是生成产物，不手改。
- 本图源码证据固定到 `d5474b3331ebaee7948b89e3e390c24a686819d0`，各节点列出相对路径与行号。
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
不提交截图、工具源码、node_modules 或机器绝对路径清单。本轮源码证据、产物与严格
provenance 检查通过；此前工具的 URL 安全策略阻止 file:// 预览，本批未运行新的浏览器
检查或感知验收，也不沿用旧截图/回执证明新 HTML。离线产物检查不能替代这两项验收。
生成图不代表用户数据已迁移、生产已切流、真实模型质量已达标，或运维已采集告警。

HTML 标记为生成文件，保留上游输出字节以匹配产物校验；仅该文件豁免空白检查。
JSON 与 Markdown 继续接受正常检查，不为清理模板里的空行而手改已验证的 HTML。

## 2026-10-28 收口更新

主图保留库/默认legacy/可选revision与评测全景，更新源码证据和发布门禁；独立V1图
展开来源批准、TaskStore许可/检查点、原生HTTP、不可变S3包及授权父块读取，避免将
新V1与旧v2/逐页扩展协议混成同一路径。两图均固定到上述已提交代码版本。

两图的schema、交付、严格provenance及真实浏览器自动检查通过；未做人工作图视觉
验收。主图自动路由仍有4处交叉提示；一次位置调整触发标签间距错误后按工具规则
恢复原布局，未为消除提示改语义/丢节点。独立V1图无此提示，HTML均由工具生成。
生成器模板自带的行尾空白原样保留，避免手工格式化破坏字节摘要/provenance；源码、
图源JSON及Markdown继续检查，已有pre-commit规则也排除docs生成物。
原始自动验收凭据在私有验证目录，机器验收记录保留交付摘要；未发布到远端。

当前实现与使用边界见[接线说明](../mineru_v1_integration.md)，完整测试/反向与未关闭
门禁见[收口报告](../evaluations/mineru_wiring_20261028.md)。默认仍关闭，未切生产配置。
