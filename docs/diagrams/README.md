# 交互架构图

[打开 Comet-RAG 交互架构图](comet-rag.html)。HTML 可直接用浏览器离线打开，支持节点
聚焦、缩放、深浅主题和导出；无需启动项目或安装前端依赖。

图聚焦库入口、当前入库主链路、可选维护与仓库离线工具，不穷举 import、部署副本或检索调用。
维护默认关闭，页进度和错误可查询；准备意图覆盖移交前取消。默认 legacy 保持原路径。
第二十一批增加独立 RevisionIngestRunner 节点，标出启用条件和对 Task/Document/VectorStore
的实际 IO，默认不打开新写入或定时补偿；接线能力不代表用户资料已迁移或生产已切流。
第二十批标出 API 准入可暂停及缺载荷保留；暂停不停止已受理任务，也不证明远端排干。
第二十二批增加独立 revision-preflight CLI 节点：只读四方配置，不向 Store 画 IO 箭头；
卡片区说明隔离重建与切读/增量回退演练，不表示项目含自动流量控制器或生产已切换。
第二十三批补原生 30 秒信号排干、超时取消与独立进程 SIGKILL 恢复旁注；维护调度仍
独立、默认关闭。单机进程信号不代表跨主机、断电或用户生产发布已验收。
图中的 IO worker cron 节点只表示可选 revision 对账；通用租约回收在 ARQ worker
默认启用，没有在这张聚焦入库与 revision 的组件图中另画节点。
第二十四批增加独立检索评测 CLI：固定语料/标签、纯评分器和复用链路的采集器。其内存
索引独立于右侧生产 Store，不画伪共享 IO；真实模型入口只在给定配置后访问模型服务。
第二十四批补验从现有 .env 选取模型项，在内存索引完成六组真实模型对照，校正标签为 15 条
证据并记录实际重排。作者标签待独立复核，权重 unknown、候选池极小，不代表生产质量
已通过；标题孤块/实体混淆和逐查询结果见 [实测报告](../evaluations/model_retrieval_20261025.md)。
第二十五批增加独立长文/困难负例集及原生测试后端对照，未采用有回退的标题合并。
生产仅修正跳级祖先路径，保留原文与硬边界；评测可注入既有测试 Store，由调用方精确
清理，不代表图右侧生产 Store 被评测使用。见 [挑战报告](../evaluations/retrieval_challenge_20261025.md)。
第二十六批补同池重排标题视图实验：page/section 与原文区间不变，但电话事实的首位
正文证据在两轮均回退，完整/祖先路径都未接入生产。录制/重放属于图左侧仓库工具，
不代表运行时会增加标题或修改右侧索引。见 [标题上下文报告](../evaluations/title_context_20261025.md)。
第二十七批补真实PDF的独立参考页、盲审包和正文token预算对照。生产MinerU、人工
独立复核与固定权重仍待验收；正文预算投影不等于联合提示词或推理成本，未默认启用
父块或改分块。见 [PDF报告](../evaluations/pdf_page_20261025.md)。
第二十八批增加独立 PDF 原生解析核验节点及其显式 HTTP 调用。MinerU 节点表示文档
API 角色，核验使用独立地址，不证明生产部署已改配；模型后端不能替代文档服务。
完整 7/80 页文件均解析但回退 section，真实三页子集保留显式原页映射；独立人工、
固定工件和完整复杂页仍待验收。见 [原生解析报告](../evaluations/mineru_pdf_20261025.md)。
第二十九批更新MinerU节点的部署侧原生渲染、安装守卫和HTTP消费来源；没有新增
应用内SDK或中间件。可选扩展下完整7/80物理页已验，未声明扩展仍保持旧路径；独立
人工、固定工件和正式部署不因此关闭。协议、故障和真实页结果见
[完整页事实报告](../evaluations/mineru_pages_20261025.md)。
第三十批在现有PDF工具节点中区分显式解析与离线审计；HTTP箭头只属于解析入口。
引擎节点补覆盖校验调用证据，没有新增服务、Store或模型路径。原生引用与整表/行
诊断不评分、不批准标签；默认分块指纹不变，见
[原生证据报告](../evaluations/native_evidence_20261025.md)。
第三十一批在PDF工具节点补冻结/读取/盲审生成的实际文件IO来源，仍不增加生产Store
或模型连接。两轮原生产物与完整物理页守卫已验；独立裁决、工件与预算门禁分开保留，
见[原生基线报告](../evaluations/native_freeze_20261026.md)。
第三十二批在PDF工具节点补复核表的实际读取与字面校验来源，不新增生产调用。
结构检查、独立裁决和固定模型工件分别验收；复核工具不会自动批准金标，见
[复核与工件报告](../evaluations/native_review_20261026.md)。
历史载荷处置、新命名空间重建和生产切换门禁见 [迁移边界](../revision_migration.md)。
详细恢复流程仍见 [架构](../architecture.md) 和 [对账设计](../../tasks/v2_reconciliation_design.md)。

## 源数据与证据

- [comet-rag.architecture.json](comet-rag.architecture.json) 是可编辑的图源；HTML 是生成产物，不手改。
- 本图源码证据固定到 `e64ddc78d8efca4053a45a89dacfcd8c562a93ac`，各节点列出相对路径与行号。
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

第三十三批补充空物理页解码和原生表格对照的已提交源码证据；用户PDF只保留数字结论，
见[探索报告](../evaluations/native_quality_20261026.md)。离线验约不冒充浏览器/视觉验收。

第三十四批说明真实MinIO双模式、全量300项中间件和两种预算口径，见[验证报告](../evaluations/validation_20261026.md)。未新增组件或生产依赖。
