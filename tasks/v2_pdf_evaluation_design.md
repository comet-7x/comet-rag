# 第二十七批：真实 PDF/page 与评测边界

基于 `7a7b8e8`。先冻结样本、拟定标签和比较条件，再调用真实模型。

## 来源与独立复核

NIST SP 800-145（2011，完整7页，选物理页5–7）与 SP 800-144（2011，完整80页，
选物理页13–20、52）。两份原 PDF 明示不受版权保护；保留 NIST 归属，固定官方 URL、
原始字节 SHA-256、页数和 pypdf 6.10.0 原生文本提取结果。正文不是生成式仿真 PDF，
也不是生产 MinerU 产物。扫描页/OCR、中文多栏和真实用户资料仍未覆盖。

24条中文问题、20条正问题、4条无答案、21份原文证据。页内 quote 必须唯一；跨物理
页的 PaaS 和 Backup 段分成两份证据，不强行拼成一个块，不移除脚注/页眉来制造连续性。
所有标签是本批作者拟定，未读取模型排名。独立人工复核仍 pending，用户已选择先用
公开 PDF 制作盲审包。包里只含问题、来源、物理页文本与空白填写表，不泄漏作者的
相关度、no-answer 分类或模型结果；独立性最终需人工确认，不由 reviewer 字符串证明。

## 冻结比较

独立 InMemory，复用现有 IndexPlanner/RevisionStager/激活/RetrievalService。只比较
hybrid，top_k=5/fetch_k=50；recursive、page、hierarchy child、hierarchy parent，分别
无/有真实 reranker，共8组/192次 search。child=800字符、overlap=80、parent=1600字符；
这是本批固定实验设置，不是默认生产参数，也不把不同语料的历史分数混入。

索引/返回原文区间不变，page与hierarchy必须保留物理页边界；recursive不承诺页边界。
未知页事实不猜测。模型、客户端与进程闸门复用；不创建应用Context或持久化索引。
先看全部逐查询、文档/原文证据分歧与模型输入成本，再决定是否有足够证据修改生产。
作者标签、小型公开资料与独立内存均不足以单独批准生产默认策略。

## 权重和 token 边界

现有 embedding/reranker 的 /models 可用，未返回权重/tokenizer revision。模型名称和
服务端max_model_len不是权重锁。固定权重验收需部署方提供两模型权重、tokenizer与
chat template的不可变版本/摘要，以及服务端版本和影响截断的配置；本批继续unknown。

原生 /tokenize 可用，统计“每份返回正文独立编码后的token数之和”，以同一计数模型
对全部策略应用512/1024预算。只保留排序中的完整前缀：首个超额块使前缀停止，不跳过
大块找后面的短块，不切断正文、不更改引用区间。预算投影不再沿用原search的耗时。

这些投影不会改变已经发生的召回/重排，不能叫相同推理成本或联合提示词预算：query、
联合指令、chat template的额外token与模型端潜在截断另算；独立prompt的默认token按实计数。单独文本计数也不等于reranker
的联合query/document token。开始/结束的固定多语言token探针只观察当前计数行为稳定
性，不能据此宣称tokenizer工件或模型权重固定。不通过字符/token换算猜token预算。

## 本批前置限制

既有MINERU_BASE_URL的 /health 返回404，且不符合MinerU非回环TLS校验；不绕过
既有安全契约上传PDF，不把Poppler/pypdf参考提取接成服务的隐式fallback。生产解析、
真实MinerU页事实和目标部署验收仍待正确protocol v2配置。H/I、M4-T10均不因此关闭。
