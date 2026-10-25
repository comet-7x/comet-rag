# 真实 PDF 参考评测

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
