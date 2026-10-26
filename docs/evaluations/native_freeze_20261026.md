# 第三十一批：冻结原生产物与证据复核输入

基于 `768261a`。本批先处理第三十批留下的可复现性与完整页核验问题，生产分块及
索引没有改动。长期任务进度见[收口记录](../../tasks/v2_closeout.md)。

## 已确认问题

两轮7/80页的原生Markdown原先只保存在临时目录，不能作为长期评测基线。另复现
原生页有序、正文重建及报告自洽仍允许漏中间页；两项故障测试先失败。本批让严格
完整页核验与原生产物加载要求连续1..总页数，普通部分页诊断保留。

## 产物与协议

[两轮原生产物](../../evals/pdf/native/README.md)分别保存完整原始JSON字节和最后写入的
manifest，锁定来源报告/正文/page字节及canonical摘要。b29与b30正文有真实差异，
没有将不同轮次混作一次模型对照。来源URL、原PDF SHA和总页数继续复用既有来源锁，
不改变旧pypdf样本和作者标签。

冻结工具先验来源、页事实与canonical，保存字节后再验存储结果，最后落manifest；
输出必须为新目录。半写入、摘要变化、未知/缺失文件名、路径穿越、bool版本或错误
canonical明确失败。校验和证明文件一致性，不能认证签名、远端权重或人工身份。

盲审默认使用b30原生正文，问题仍仅基于12个选定物理页。导出24题、选定页全文与
完整canonical字符偏移、空白复核表，不导出原作者相关度、no-answer分类、quote或
模型排名。其余75页不改变本次问题可回答性。包身份绑定正文、问题文本与页范围。

证据role为passage/table_header/table_row；每片段记录document_id、物理页、
start/end和逐字quote。表头+数据行或跨页答案分列并注明联合需求，不复制表头入原文，
不把不连续片段拼成连续引用。填写reviewer、时间和独立性说明后仍需人工裁决；工具
不凭字符串自动批准独立标签。解析失真需记录并对照原PDF，不为评分修补canonical。

来源是两份已核验公开NIST出版物，原PDF Authority声明与
[官方Technical Series许可](https://www.nist.gov/open/copyright-fair-use-and-licensing-statements-srd-data-software-and-technical-series-publications)
再次复核，保留作者/版本/归属；不是对NIST全站第三方材料的通用授权。机器转写存在
误差，不替代权威出版物，也不提供当前安全/合规建议。

## 验证

定向56项通过，11种独立源码变异均被目标断言检出。首次字节变异未检出：旧测试使用
损坏JSON，后续解析仍会拦住；已换成合法JSON只增加空白，确认字节锁本身会失败。
测试副本与故障日志单独保存，不把测试收集/夹具错误算作检出。

首次普通unit3561 passed/15 skipped/1 xfailed，11.20s，明确未达10秒；随后新增3个
故障用例。最终插桩unit3564 passed/15 skipped/1 xfailed，15.56s，覆盖89.49%；
integration298 passed/1 skipped（MinIO）/59条上游ARQ警告，668.29s。E2E50 passed/
4.14s、benchmark39 passed/8.38s；合并92.86%，tasks904/943=95.86%。单测预算留
第三十四批治理，不放宽等待或用后续最快值覆盖本次记录。

Ruff/Pyright、7个改动Python文件格式、复用core-only环境的库/DOCX冒烟通过；原PDF
由bundled pypdf6.10.0核验原字节/页数/参考提取文本，Authority物理页5/11复核。
首次误用MinerU工具环境的pypdf6.21.2被版本守卫拒绝，未升级服务或改锁来通过。

严格完整PDF实测7/80页，8.07s/118.85s，规范化10066/220912字符；本轮产物独立保存，
未替换冻结的b29/b30或沿用旧区间评分。临时API5任务完成、零失败，排空后SIGTERM，
PID/监听均消失；保留上游1条semaphore清理警告。专用PG/Redis两个容器、两个卷与
网络精确清理，Milvus集合1→1。测试PG关闭fsync，不构成断电验收。

临时证据位于`/tmp/comet-v2-batch31-validation/`；新盲审包可从已提交产物重新生成，
压缩包保存于同目录native-blind-review.zip。图文校验另行记录。

## 后续边界

第三十二批需要复核表的严格输入检查、逐条裁决及不可变模型工件。未收到独立人类
结论和工件清单前，不把新包当成已冻结金标。正式目标部署、M4-T10/H/I和发布条件
继续逐项保留；本批基线固化不改变生产开关。
