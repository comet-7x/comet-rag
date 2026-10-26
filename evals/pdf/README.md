# 真实 PDF 参考评测

第三十三批的原生表格局部对照入口为
`uv run python -m evals.pdf.table_experiment --config /path/to/models.yaml --out /tmp/comet-table-new-run`。
只比较冻结b30原生12页域中的两条作者暂定表格题，非原24题重标。默认与显式
`</tr>`分隔符配置使用相同字符参数/返回正文token预算；候选池因分块不同而不同。
长于子块上限的行仍可能切开，联合片段指标不是答案准确率，生产默认不变。

自定义来源调用 `artifacts.freeze(..., data=custom_data, attribution=...)`，必须显式
提供非空归属，不能继承NIST许可。逐页空记录保留物理页；内容块只覆盖规范化非空页。
原生报告用 `normalized_empty_page_numbers` 声明空页，旧NIST报告默认空列表仍兼容。
用户资料及派生正文留在Git忽略目录。两轮解析波动与质量边界见
[本轮报告](../../docs/evaluations/native_quality_20261026.md)。

这里保存公开 PDF 的原生文本页快照和**作者拟定标签**，不保存 PDF 二进制，也不接入
生产提取器。方法与限制见 [第二十七批设计](../../tasks/v2_pdf_evaluation_design.md)。

## 来源与许可

- Mell P, Grance T (2011), *The NIST Definition of Cloud Computing*, SP 800-145，
  [官方发布页](https://csrc.nist.gov/pubs/sp/800/145/final)。原文件7页，选物理页5–7。
- Jansen W, Grance T (2011), *Guidelines on Security and Privacy in Public Cloud Computing*,
  SP 800-144，[官方发布页](https://csrc.nist.gov/pubs/sp/800/144/final)。原文件80页，
  选物理页13–20与52；52是表2所在的物理页，印刷页码为42。

两份原 PDF 的 Authority 分别在物理页5、11明确表示不受版权保护。NIST 的
[Technical Series 许可说明](https://www.nist.gov/open/copyright-fair-use-and-licensing-statements-srd-data-software-and-technical-series-publications)
要求保留推荐归属；不将第三方同站内容一概视为可复制。

Republished courtesy of the National Institute of Standards and Technology.

这是2011年历史技术文本的检索样本，不代表当前安全、法律或合规建议。原始URL、完整
文件SHA-256、字节数、总页数、选定页和pypdf版本记录在 [pages.json](pages.json)。
span是选定页拼接并规范化后的字符位置，不是PDF字节或矩形坐标。
selected页保留原始行序、图中文字、脚注和页码；抽取中的拆词/私用区项目符号没有按排名
修正。未选中的页面不属于本次可回答范围。图像、几何表格、扫描OCR、中文多栏未验收。

## 运行和复核

```bash
uv run python -m evals.pdf --review-out /tmp/comet-pdf-blind-review
uv run python -m evals.pdf --config /path/to/models.yaml --model-revision unknown \
  --out /tmp/comet-pdf-real
uv run pytest tests/benchmark/test_pdf_quality.py -m benchmark
```

盲审输出只包含问题、来源/页文本和空白表；无作者相关度、no-answer分类或检索结果。
问题ID不编码分类。reviewer为空、状态pending、逐题decision=unreviewed；空相关度不能
当作已确认无答案。请由未参与本批拟定标签的人阅读PDF，逐题标记
answerable/unanswerable/uncertain、文档等级、物理页码、原文quote和分歧。与作者标签
裁决后再发布新金标；工具不凭填写名字认证人的独立性，也不自动批准生产质量。

24条问题中作者拟定20条可回答、4条无答案，21份页内证据。PaaS/Backup跨页段分列两份
证据；不得跨两个返回碎片拼成单块证据。完整性由既有严格评分器执行。
原始合成集与挑战集保留；本集指纹独立，不覆盖历史快照。

原PDF可按pages.json的官方URL下载到外部目录，文件名分别nist145.pdf、nist144.pdf。
用已安装pypdf 6.10.0的独立文档工具环境核验，不给服务/核心依赖加PDF库：

```bash
/path/to/pdf-tools/python -m evals.pdf.verify --pdf-dir /path/to/source-pdfs
```

该命令只核验原字节、实际物理页和冻结提取文本，无网络访问，也不导入服务/模型包。
快照加载和常规CI无需pypdf；核验器拒绝空样本、空页与逆序页，不把零次检查当作成功。

## 模型与预算

评测复用组合根工厂、模型闸门和既有入库/读取阶段，只用独立内存索引。CLI需要真实
reranker及其原生/tokenize端点，失败不回退估算或模型替身；模型、计数客户端在退出栈
关闭。无配置时只生成盲审包，不访问模型。已有运行期APPConfig可调用real.evaluate，
不需要将凭据另存为YAML；YAML入口不自动读.env。

固定hybrid、top_k5/fetch_k50、child800/overlap80/parent1600字符，比较recursive、page、
hierarchy child/parent及重排开关。字符设置是本批实验参数。每次真实search保留原top5，
另按原生raw-text计数投影512/1024预算，只保留完整排序前缀，不跳过大块找后面的短块。
预算后的run清除时延；预算是各返回正文独立编码的token数之和。原生prompt接口默认
添加的token按实计入；未计联合请求的query、指令、chat template等额外开销，不等于
服务端联合输入、embedding或重排总计算成本。

模型名称、/models.max_model_len和token探针稳定均不能证明权重锁定。当前两模型
revision未知；完整权重、tokenizer/chat template、服务版本和截断配置需部署方提供
不可变标识。model_revision参数只是调用方声明，本工具不验证远端工件；无法确认就
填unknown。任何好分数都不能抹掉独立复核、生产MinerU、固定权重或目标部署门禁。

## 独立复核填写约定

仅依据sources.json列出的选定页作答；其他页的答案可写notes，但不算本样本可回答。
逐题decision填写answerable、unanswerable或uncertain，不把“不确定”写成“无答案”。
relevance是document_id到0/1/2等级的映射：2为主要答案来源、1为补充、0为不相关。
evidence每项填写document_id、page_number（物理页）与逐字quote；跨页答案分列，不能
将两个片段合成一条不存在的原文。无答案题也需notes说明检查范围和缺失依据。
填写reviewer与复核日期/角色说明；提交后先裁决分歧再冻结版本，不直接覆盖作者金标。

## 原生 MinerU 解析核验

```bash
uv run python -m evals.pdf.parse --pdf-dir /path/to/source-pdfs \
  --mineru-url http://127.0.0.1:8989 --backend vlm-http-client \
  --out /tmp/comet-native-pdf-new-run
```

这是显式访问外部文档服务的独立工具，不需要 pypdf，不接入生产索引。URL 遵循现有
MinerU TLS/回环规则。先核验全部原 PDF 字节与来源锁，再按单任务顺序上传；解析后
复核来源字节，保存实际 Markdown、原生页片段和报告，不保存端点或凭据。输出目录
必须是新目录，避免失败重跑旁边保留旧成功报告。

`page_status=verified` 只表示页号在该 PDF 内、原生正文精确重建、规范化及分块硬边界
通过；没有页事实写 `unavailable_section_fallback`，不会声称页验收成功。工具不证明
OCR 字词正确、所有物理页均被识别、人工标签有效或模型权重固定，也不跨 canonical
正文复用旧字符 span 评分。来源锁、正文子集映射与整份回退观测见
[第二十八批报告](../../docs/evaluations/mineru_pdf_20261025.md)。

要求完整复杂PDF页事实时，加 `--require-pages`；没有原生页事实会直接失败，不将
section回退写成成功报告。完整公开文件验收需文档服务具备已验证的逐页能力，见
[独立部署扩展](../../deploy/mineru/README.md)。第二十九批补齐完整7/80页原生核验，
不改变旧参考快照和标签；报告见[完整页事实验证](../../docs/evaluations/mineru_pages_20261025.md)。

## 原生 canonical 离线审计

```bash
uv run python -m evals.pdf.inspect --native-dir /path/to/parse-output \
  --out /tmp/comet-native-inspection-new-run
```

只读取本地 report.json 和逐文件 Markdown/page JSON，不访问 HTTP、模型或生产索引。
输入须通过来源锁、完整物理页、全文重建及规范化报告一致性检查；可编辑报告不证明
远端身份或固定工件。输出必须是新目录。

`audit.json` 给出旧引用在新 canonical 的严格对齐候选，以及默认800/80/1600字符下
HTML整表/显式整行是否被一个真实返回单元覆盖。引用须唯一（含重叠重复）且在原期望
物理页；不匹配留空，匹配仍 pending，不做模糊迁移或独立批准。表格只支持独立行首
HTML table/tr，忽略代码围栏，坏表格明确失败；不覆盖所有Markdown或单元格语义。
这是诊断报告，不是盲审包或评分结果。旧选定12页问题不能直接推广到完整87页，
也不能沿用旧字符span或把多个碎片拼成单块证据。

冻结产物结果、覆盖守卫与下一步边界见
[第三十批报告](../../docs/evaluations/native_evidence_20261025.md)。

## 冻结原生产物与新盲审包

[原生产物](native/README.md)按轮次提交原始JSON及manifest；不再依赖临时目录作为
基线。`load_frozen`先校验精确文件清单、字节摘要，再复用完整页和canonical检查。
部分写入没有最终manifest，不能成为有效快照。正式复核应固定仓库提交及轮次，
自洽的可编辑manifest不是签名或模型工件证明。

```bash
uv run python -m evals.pdf.artifacts freeze --native-dir /path/to/parse-output --out /new/frozen-dir
uv run python -m evals.pdf.artifacts review --out /new/blind-review-dir
```

freeze与review都拒绝覆盖已有输出。默认盲审采用b30原生正文、原问题的12个物理页；
以完整原文偏移标注passage/table_header/table_row。联合证据分列，禁止拼造连续quote。
完整原生页验收现在要求1..总页数，页有序、范围合法和报告自洽仍不足以证明没有漏页。

## 回收复核表的结构校验

```bash
uv run python -m evals.pdf.review --packet /path/to/blind-packet \
  --response /path/to/submitted-review.json --out /new/validation-dir
```

复核人填写status=submitted、reviewer、YYYY-MM-DD日期与独立性说明，并逐题填写
answerable/unanswerable/uncertain。工具重新生成冻结输入，检查问题/来源未被替换，
严格匹配canonical与问题页域、完整问题集合、KB相关度、页内原文区间与逐字quote。
重复片段、bool页号/等级、拼造引用和没有说明的联合片段被拒绝；无答案需说明范围，
不确定不会升级成可进入裁决的完整结果。输出摘要对应同一份已验证响应字节。

校验通过只表示结构与字面事实有效：independent_review仍为unverified，
quality_acceptance仍为pending。ready_for_adjudication不是独立审阅认证、分歧裁决
或金标冻结；角色字符串不能证明人的独立性，模型名称也不能证明固定权重。
