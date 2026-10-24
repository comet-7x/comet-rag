# Skills 与 RAG 生态复核

> 调研日期：2026-10-24。仓库基线 `3c3699d`。读取项目官方 GitHub 元数据、README、
> 下列 SKILL.md、指定绑定源码与许可证；结论针对当日默认分支，不能代替目标发布版验收。

## 结论与证据范围

Comet-RAG 已有清楚的库/服务分离、Port 和故障契约。近期应完成 revision 生产恢复协议，
而不是更换整套框架。文档采用轻量 Markdown/Mermaid、读者入口和少量明确开发规则；
框架作为设计参考，解析器与评测器作为独立实验候选。没有安装 skills、修改依赖或接入后端。

下面区分三种证据：**上游能力**来自官方说明或所列源码；**本项目现状**来自仓内实现与
既有测试记录；**适用判断**是本次推论。未在本项目运行任何候选解析器或框架，不认领
上游 README 的速度、质量分数或排名，也不宣称审计过其全部源码。

## 一、文档与开发 skills

| 来源 / Skill | 核对内容 | 对本项目的采用方式 |
|---|---|---|
| [softaworks：c4-architecture](https://github.com/softaworks/agent-toolkit/blob/main/skills/c4-architecture/SKILL.md) | 按读者区分 Context/Container/Component，避免库被误画成独立部署单元 | 借鉴视角区分，保留现有架构文档；不为满足模板再建四套目录 |
| [softaworks：mermaid-diagrams](https://github.com/softaworks/agent-toolkit/blob/main/skills/mermaid-diagrams/SKILL.md) | 文本图、流程/时序/状态图及单图聚焦；同时核对 flowcharts/sequence 参考 | 本轮采用普通 flowchart 更新系统边界和 revision 切片图；不增加 JS 构建依赖 |
| [softaworks：crafting-effective-readmes](https://github.com/softaworks/agent-toolkit/blob/main/skills/crafting-effective-readmes/SKILL.md) | 以读者与任务选择 README 内容，更新前先核对真实入口 | 借鉴用户/开发者分流；修正已过时状态和服务示例前提，不再写一份大而全使用手册 |
| [Anthropic：doc-coauthoring](https://github.com/anthropics/skills/blob/main/skills/doc-coauthoring/SKILL.md) | 上下文、结构迭代、读者问题检验 | 借鉴读者检验；已有仓库证据不重复问卷。其[仓库说明](https://github.com/anthropics/skills/blob/main/README.md)区分开放与 source-available 内容，不把整个仓库视为统一许可并复制进来 |
| [wshobson：python-code-style](https://github.com/wshobson/agents/blob/main/plugins/python-development/skills/python-code-style/SKILL.md) | 类型、命名、自动格式、文档规范；同时有 strict/行宽等通用模板 | 保留现有 Ruff/Pyright 配置及中文注释标准，不照搬 strict、120 列或全量长 docstring |
| [wshobson：python-design-patterns](https://github.com/wshobson/agents/blob/main/plugins/python-development/skills/python-design-patterns/SKILL.md) | KISS、职责分离、组合优先、删除死代码后再抽象 | 采用“实际变化点解释抽象成本”；不把三次重复、函数行数或构造参数数量当硬门槛 |
| [wshobson：architecture-decision-records](https://github.com/wshobson/agents/blob/main/plugins/documentation-generation/skills/architecture-decision-records/SKILL.md) | 记录背景、选择、后果与被替代状态 | 重大新决策按此结构写；既有 spec/M4 决策不复制成一套 ADR，不额外安装 adr-tools |

softaworks 仓库为 [MIT](https://github.com/softaworks/agent-toolkit/blob/main/LICENSE)，
wshobson 仓库元数据标记 MIT；本轮只借鉴方法并自主编写文档，没有 vendor 第三方 skill。
这些模板中的通用建议不能覆盖本仓 spec：例如同步/异步命名、层级白名单、生产开关及
确认边界都沿用现有规则。也不为文档任务生成整仓知识图谱、注册 Git hook 或引入文档站。

## 二、框架、解析器与评测工具

每项的“参考/试验”是适用判断，不是批准安装。代码许可与模型权重/托管服务许可分别核对。

| 项目与一手来源 | 核对到的定位 / 许可 | 值得借鉴或试验 | 当前决定 |
|---|---|---|---|
| [RAGFlow](https://github.com/infiniflow/ragflow/blob/main/README.md) | 完整 RAG/Agent 产品；当前源码启动入口为 Go，另有前端及解析/存储依赖；仓库 Apache-2.0 | 解析模板、可追溯引用、用户验证解析质量的流程 | 作为产品与评测参考，不作为 Python 核心库依赖，不复制整套部署 |
| [MinerU](https://github.com/opendatalab/MinerU/blob/master/README.md) | 文档解析/OCR 工具链，当前主分支涵盖更多格式；[自定义许可](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md)基于 Apache-2.0 并附条件 | 页/块定位、表格公式、解析错误与明确的质量选择 | 保持本项目外部 HTTP 适配；先测目标服务版本，不把新 CLI 当成已兼容现有 `/health`、`/tasks` 协议 |
| [pdf-inspector](https://github.com/firecrawl/pdf-inspector/blob/main/README.md) | Rust PDF 分类、位置文本/Markdown 提取；选择性 OCR 路径及 Python/Node/WASM 绑定；MIT | OCR 需求预检、原生文本 PDF 轻路径、页级来源 | 适合独立对比；暂不加入 engines，先验证 GIL、事件循环、取消和中文复杂页 |
| [anydoc](https://github.com/firecrawl/anydoc/blob/main/README.md) | Rust 多格式文档到 Markdown，Python/Node/WASM；MIT；PDF 复用 pdf-inspector | 统一内部表示和序列化、多格式输入契约 | 作为 Office/多格式对照，不替换已稳定 DOCX；hosted OCR 不作为自动降级 |
| [LlamaIndex](https://github.com/run-llama/llama_index/blob/main/README.md) | MIT 的 OSS framework 与独立 integrations；当前 README 明确公司重点转向 LlamaParse/LiteParse/评测 | core 与 integration 包拆分、接口发现性、检索/解析对照 | 不将 LlamaParse 云平台能力等同 OSS 框架；不整体迁移编排层 |
| [LiteParse](https://github.com/run-llama/liteparse/blob/main/README.md) | 独立轻量 PDF 解析、bbox、可接 OCR、worker pool；Apache-2.0 | PDF 预检、原生文本提取与进程隔离超时 | 与 pdf-inspector 放进同一离线语料比较；不假定其跨文档并行模型已适配本项目 |
| [Haystack](https://github.com/deepset-ai/haystack/blob/main/README.md) | Python 模块化编排，支持原生 async、分支/路由；Apache-2.0 | 透明的组件边界与调度，参考其集成测试组织 | 不再加一层通用 Pipeline，也不回迁到同步/异步混在同一类的 API |
| [Docling](https://github.com/docling-project/docling/blob/main/README.md) | 文档转换；代码 MIT，模型另按各自许可 | [DoclingDocument](https://docling-project.github.io/docling/concepts/docling_document/) 的层级、阅读顺序、bbox 与 provenance | 借鉴字段语义和评测样本；适配后仍输出本项目 Extracted/NormalizedDocument，不让供应商类型穿透 Port |
| [Marker](https://github.com/datalab-to/marker/blob/master/README.md) | 文档转 Markdown/JSON/chunks；当前代码 Apache-2.0，权重是附限制的修改版 OpenRAIL-M | 数字文本/选择性 OCR、页块结构及解析对照 | 仅独立实验或另提外部服务方案；不因代码许可或 CPU 模式而忽略 PyTorch/模型运行时和权重条款 |
| [Ragas](https://github.com/vibrantlabsai/ragas/blob/main/README.md) | 离线 LLM/RAG 评测工具，Apache-2.0 | context precision/recall、噪声敏感性；按需加 LLM judge | 先作独立评测环境，不导入在线查询路径，不用生成答案指标代替当前检索验收 |

### 三个会改变选型的细节

**Rust、释放 GIL 与原生 async 不能混为一谈。**
[anydoc 的 Python 绑定](https://github.com/firecrawl/anydoc/blob/main/python/src/lib.rs)
中，to_markdown/to_markdown_bytes 使用 `py.detach`，但它们仍是同步调用。
[pdf-inspector 的绑定](https://github.com/firecrawl/pdf-inspector/blob/main/src/python.rs)
中，所核对的普通 process_pdf/process_pdf_bytes 直接调用解析；OCR 入口显式使用
allow_threads。由这些源码推断，不能直接声称普通提取放进 to_thread 就已解决事件循环
隔离。若试验：先查目标版本是否有原生 async；无则测 GIL 与长文档心跳，再决定有界
线程或进程；取消不能释放仍被后台解析使用的文件。上述判断未经过本项目基准验证。

**“本地转换”不意味着所有选项都本地。** anydoc 默认本地转换，但显式 hosted OCR
会把需要 OCR 的整份文件发给 Firecrawl Parse，当前接口没有页选择。
pdf-inspector 的 OCR 路径也需要外部 PDFium/ONNX Runtime/模型文件；默认提取与 OCR
运行时是不同依赖边界。候选试验必须关闭隐式云端处理，不能替换现有明确的 MinerU 路由。
来源：[anydoc OCR](https://github.com/firecrawl/anydoc/blob/main/README.md#ocr)、
[pdf-inspector Python API](https://github.com/firecrawl/pdf-inspector/blob/main/docs/python.md)。

**许可必须按目标版本和具体资产核对。** 当前 MinerU 主分支不是旧资料里常见的简单
AGPL/Apache 标签，其 LICENSE 另列商业门槛和在线服务标识条件；外部 HTTP 接入也不能
被描述成自动免除所有条款。Marker 代码与权重许可不同；Docling 也明确模型另有许可。
本轮记录事实与原始链接，不据此作商业合规结论或批准引入。

### 如何让试验产生有效结论

从同一固定语料开始：原生 PDF、扫描/混合页、多栏中文、表格、公式、损坏编码、复杂
DOCX 与超限输入。固定版本、硬件、选项，禁用不透明云回退；分别记录冷启动、稳态、
P50/P95、峰值内存、页块/字符定位准确度、结构完整性和失败类型。

本项目已有 DOCX 快照、ArchiveLimits、DocumentBlock span 和资源取消契约。新工具的
Markdown 看起来更短或演示更快，不证明这些契约成立。anydoc 与 pdf-inspector 也有
依赖关系，不能把它们算成两套完全独立算法进行投票；各家自报 benchmark 不直接横比。

评测先测固定标签下的 Recall@k/MRR/nDCG，再视问题加入
[Ragas 指标](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/)。LLM-based
指标可能调用模型；评测需明确模型、成本与数据去向。先做独立离线环境，不污染 unit
零网络约束，也不把生成答案的 faithfulness 分数写成当前检索服务已交付能力。

## 三、本轮如何落地

- 在已有 [structure.md](../docs/structure.md) 中补系统边界和独立 revision 流程图，
  校准检索图中的判活与 deadline；不新增第二套架构站。
- 新增 [CONTRIBUTING.md](../CONTRIBUTING.md)，把已有仓库边界与可读性原则整理为
  可版本控制的开发规范，细节继续链接既有注释/测试文档。
- 修 README 的当前状态、用户入口与服务示例前提。库用法仍由 pipeline_usage 维护，
  服务配置仍由 deployment/config.example 维护，不复制完整手册。
- [v2_next_steps.md](v2_next_steps.md) 维护下一步优先级；v2_plan 继续保存历史证据。
  本轮是研究和文档校准，不编号为第十七批运行时改造。

后续只有在固定样本证明收益、执行模型满足取消/并发约束、许可和依赖边界清楚后，
才提出具体适配变更。无需为了“借鉴优秀项目”换掉已经成立的本项目契约。
