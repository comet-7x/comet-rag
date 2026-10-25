# 检索质量评测

本项目先评估检索返回的文档与证据。当前没有答案生成链路，不评估答案正确率或
faithfulness。框架延迟基线仍见 [benchmark](benchmark.md)，两者分别归档。

第二十四批建立的是可重复的合成语料回归集，还不能作为生产质量验收。本轮已补
[真实模型实测与逐查询复核](evaluations/model_retrieval_20261025.md)，结果仍受合成语料、
未知权重版本和 InMemory 精确召回的范围限制。

## 固定语料与标签

[corpus.json](../evals/retrieval/corpus.json) 描述虚构的星桥、港湾运维手册，共 7 份版本
文档：3 份 DOCX、4 份 Markdown，属于 ops/lab 两个知识库，其中一份是保留在索引中的
旧 revision。生成器从标题、段落和表格生成原文件，DOCX 再经过真实 extractor；两种
格式都经过现有 MarkdownDocumentNormalizer。不提交生成的 DOCX、运行结果或用户文档。

[judgments.json](../evals/retrieval/judgments.json) 独立维护 18 条查询、15 条证据：16 条
有答案、2 条无答案，包含事实、改写、数值、表格、多文档与跨环境干扰。文档身份为
稳定的 id，并映射到 `(kb_id, source_id, revision)`；revision id 全局唯一。每个 source
恰有一个 active 版本；查询只能将本 KB 的 active 文档标为相关。

标签先于检索实验编写，不读取模型输出或 chunk ID。相关度 2 表示主要答案文档，1 表示
补充答案文档，0 表示不相关；这个小型封闭集合中，未列出的 active 文档按 0 处理。
这是本批作者编写的标签，**尚未经过用户或独立双人复核**，不是外部权威数据集。
新增真实资料时应先盲标、复核分歧并补齐困难负例，再冻结用于比较的版本。

真实模型实验前自审校正了 retention_compare 的平等相关度（2/2），并为两条容量表查询
补列标题证据；原来只有数据行的标签不够完整。首版 14 条证据和旧指纹保留在历史记录，
新旧分数不能混评。本次自审不替代上述独立标注验收。

证据用完整原文 quote 标识，必须在规范正文唯一出现，生成时定位为半开字符区间
`[start, end)`。标签不随 chunk 策略生成：同一答案被切断会降低证据覆盖率，不能靠重标
chunk 抹掉问题。标识、active 状态、表格列数、证据引用和查询集合均严格校验。

## 评分规则

评测沿用 IR 中的相关性评估和分级排名指标；具体实现采用以下显式约定，避免工具之间
使用不同分母或收益函数却比较同名指标。背景见
[Stanford CS276 评估课件](https://web.stanford.edu/class/cs276/handouts/lecture8-evaluation_2014-six-per-page.pdf)。

| 指标 | 本仓库约定 |
|---|---|
| Recall@k | 前 k 个**返回位置**覆盖的不同正相关文档数 / 全部正相关文档数 |
| RR@k / MRR@k | 首个正相关文档的原始排名取倒数；前 k 无命中为 0；macro 中 rr 是正查询的 MRR |
| nDCG@k | `gain=2**grade-1`，`discount=log2(rank+1)`；IDCG 用该查询全部正标签按收益降序取前 k |
| evidence@k | 前 k 中完整包含 required quote 的单个块所覆盖的证据数 / required 证据数 |
| returned_chars@5 | 前五返回位置的正文字符数，包含重复内容，不等同 token 或内存/磁盘占用 |
| no_answer_return_rate@5 | 无答案查询返回非空结果的比例；只描述召回行为，不代表生成模型会答错 |

文档重复出现会占据排名，但只在第一次出现获得收益，不先去重压缩排名。同一 hit id
重复则是无效 run。证据不能跨两个碎片拼接，正文必须与规范原文的 span 完全一致。
文档命中和证据命中分开，避免只召回标题也被认定拿到了答案。

无答案查询的 Recall/RR/nDCG/evidence 为 null，排除在正查询 macro 之外，单独报告。
有答案查询返回空列表是有效的 0 分；服务异常、降级、漏查询、重复查询、未知文档、
跨 KB 和 inactive 命中是无效 run，评分直接失败，不能静默过滤或从均值删掉。
每条结果还记录实际 reranked 与 fetched，并列出 covered_evidence@k 的证据 ID。
请求重排且返回非空结果时，必须观察到 reranked=true；不能只按请求配置声称重排有效。

每次记录 1/3/5 三个截断点、逐查询结果、查询数/分母和正文成本。查询计时覆盖
RetrievalService.search（含 query embedding、召回、可选重排、父块读取），不含建库、
原文生成、入库和 HTTP API 传输。P50/P95 使用 nearest-rank；每组只有 18 次、一次遍历、
没有预热隔离，P95 接近最慢样本，不能作为吞吐或稳定尾延迟验收。

## 从仓库运行

先按项目开发环境安装依赖；这些工具只存在于 checkout，不进入 comet-rag wheel，不增加
运行时依赖、HTTP 接口、Store 原语或向量 schema。所有输出要求使用**不存在的新目录**。
失败可能保留原文件或部分 run 供诊断，只有整组成功才写 summary.json。

```bash
uv run python -m evals.retrieval generate --out /tmp/comet-eval-corpus
uv run python -m evals.retrieval baseline --out /tmp/comet-eval-offline
uv run python -m evals.retrieval score \
  --run /tmp/comet-eval-offline/hierarchy-child-hybrid.run.json
uv run pytest -m benchmark tests/benchmark/test_retrieval_quality.py
```

CI 在普通单测之后单独运行这组确定性回归；它不计入默认 unit 的 10 秒预算，也不访问
真实模型。其余计时 benchmark 仍显式运行，不因计时波动让 CI 失败。

generate 输出源文件和 manifest：标签、规范正文、证据 span、生成文件 SHA-256，以及
整个 manifest 的指纹。DOCX 核心时间和 ZIP 时间戳固定。解析行为、生成文件或标签变化
会改变指纹；不同指纹禁止混评，不尝试从相似文本猜对应关系。

baseline 复用真实解析/规范化、RecursiveChunker、MarkdownSectionStrategy、HierarchyBuilder、
IndexPlanner、RevisionStager、DocumentStore 激活和 RetrievalService。旧版本先激活、随后
被新版本替换，故意不 GC；另一 KB 同名手册也保留。dense/keyword/hybrid 各比较 recursive
child、Markdown child、hierarchy child/parent，共 12 组。flat 不伪装成 parent 比较。

离线 embedding 是与标签无关的 SHA-256 字符二元组散列，256 维，无语义理解；无重排。
BM25 是现有 InMemory 实现。child size=100、overlap=15、parent size=300、fetch_k=50、
top_k=5，长度单位是字符。文件小、section 多数短于父块预算，不能据此判断真实长文
父块策略的优劣。parent 返回更多上下文，需要结合正文成本；相同 k 不等于相同 token 预算。

run 文件包含 `dataset_sha256`、`top_k`、`settings` 和 `results`；每条结果带 query_id、
hits、degraded/error、实际 reranked/fetched 和可选 elapsed_ms。hit 包含 id、document_id、start/end、text；
已知源身份必须来自检索 metadata，不能按答案内容猜。第三方采集器也可输出该格式再评分。
settings 必须声明模型、维度、后端、策略、模式、返回级别、重排开关和实现标识；本工具
额外记录模型版本声明、Python/python-docx 版本、源码指纹、索引/父块条数及正文字符量。
这些字符量不是数据库容量或真实存储增量。结果文件不写模型地址、令牌或配置原文。

## 当前离线回归基线

固定指纹为 `77b1547c8df1560faddfb5b6f23e6ae1818f42cff7fce6546ac3801687f51c21`。
[offline_baseline.json](../evals/retrieval/offline_baseline.json) 只固定确定性质量/正文成本与
条数，不固定耗时、源码指纹或机器信息。它是首次观测后审查的回归快照，指标公式另由
独立手算用例验证；不能把“快照没变”解释成“标签正确”。

| child / hybrid | Recall@1 | evidence@1 | MRR@5 | nDCG@5 | 平均返回字符@5 |
|---|---:|---:|---:|---:|---:|
| recursive | 0.9375 | 0.9375 | 1.0000 | 1.0000 | 322.7 |
| Markdown | 0.8125 | 0.8125 | 0.9375 | 0.9539 | 226.1 |
| hierarchy | 0.8125 | 0.8125 | 0.9375 | 0.9539 | 226.1 |

全部 12 组 Recall@5/evidence@5 为 1.0；recursive 索引 13 个 child，Markdown 为 22，
hierarchy 为 22 child/21 parent（均含旧版本）。hierarchy parent/hybrid 平均返回 248.6
字符，当前样本没有获得额外证据收益。这反映小样本饱和，不能据此更改生产策略。

全部无答案查询均返回结果（return rate=1.0），这是当前无拒答阈值的特征化观察，不据此
新增任意阈值。要决定阈值，需要更大的正/负查询集和误拒/误召回成本约定。
基线变化时先检查逐查询 run、规范正文、标签和解析快照；不提供无条件覆盖金标的开关。

## 真实模型与剩余门禁

提供明确的模型配置路径后，可复用组合根的模型工厂：

```bash
uv run python -m evals.retrieval real --config /path/to/models.yaml \
  --model-revision unknown --out /tmp/comet-eval-models
```

该入口只创建 embedding/reranker 客户端与共用闸门，**不创建完整 Context**；配置里的
PostgreSQL、Redis、Milvus、MinerU 和对象存储不连接。使用相同合成语料、独立 InMemory
索引和 hierarchy child，比较三种召回模式，以及配置了 reranker 时的开关对照。
model-revision 是调用方声明的部署权重/版本标识（可同时记 embedding/reranker 版本）；
不能确认时填 unknown，不能只凭模型名称声称权重版本固定。异常或降级使本次失败，
客户端在成功/失败时都关闭；不因真实模型服务不可用而退回替身。

YAML 入口不自动读取 .env。已有运行期配置可传给 evaluate_configured_models 接收已校验
的 APPConfig，不必把凭据另存为文件。本轮只从现有 .env 选取模型项，实际探测维度后
在内存构造配置，并为这两个模型地址设置子进程 NO_PROXY；未创建或修改 config.yaml。
六组实测中 dense/hybrid 的全部可见候选仅 17/3 条，不存在 ANN 漏召回；具体结果、
标题孤块、实体混淆与无答案边界见 [实测记录](evaluations/model_retrieval_20261025.md)。

这个入口能测模型在本语料上的效果，不能验收 Milvus ANN 召回率或线上吞吐。实际
模型基线应另行归档，不能覆盖离线快照。后续需补：独立标签复核、更大困难样本、真实
资料与长文、原生/扫描/混合 PDF、多栏/公式、真实页边界的 page 比较、相同 token 预算、
ANN/精确召回对照、稳定延迟与内存/存储增量、目标部署验收。M4-T10 与 H/I 保持未完成。
