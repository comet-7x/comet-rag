# 阶段 B 持久文本闭环

核验日期：2026-10-29。实现本地 TXT/Markdown、原生 DOCX、范围切分、实际表示、嵌入、PostgreSQL 内容事务和 Milvus dense/BM25，以及两条异步 Pipeline。

## 实际行为

- 文本保持编码、CRLF、空白；原生 DOCX 保留富文本、标题/列表、链接、表格合并/网格缺口、内嵌图片和原生 OMML。未映射内容给出 located partial issue，不伪造 DOCX 页码。ZIP/XML 限额先于结构展开。
- 字符切分保存原始 `[start,end)` 范围，不通过重复文字反向查找位置。结构切分保留表格/媒体原子性及自包含子内容，合成分隔符没有原文坐标。
- units 保存可返回内容，representations 保存实际模型输入。身份覆盖有序内容、scope、profile 与空间；codec 重建重新核对，拒绝悬空资产、表格子内容和错误身份。
- PostgreSQL 在一个真实事务保存完整 units、representations、manifest。相同 scope/source 只允许相同快照/配置重试；内容变化要求新 scope。
- Milvus 每个 scope 使用独立集合，绑定 profile/space/schema，原生 dense + BM25。过滤先于候选 limit。写入区分 accepted/durable/query-visible，按明确 ID 与内容指纹核对。
- 超时可能已经写入，保留清单和 unknown 状态；不删除旧数据。删除先按 manifest 的 records 明确删除和核对，再删内容，最后删清单，Asset 保留。
- keyword 不调用查询模型；多表示先按最佳证据归并 unit，再执行 RRF，保留各通道 raw score/rank。

## 真实验证证据

使用 PostgreSQL 17.11、Milvus 2.6.23、etcd 3.5.16 和本机 CLIP CPU 推理。CLIP 模型/预处理文件固定，512 维，权重 SHA 与 revision 见 [示例](../examples/README.md)。memory 只作契约测试，不替代以下持久验证。

1. PostgreSQL 五项真实集成覆盖最终 manifest INSERT 故障后的整事务回滚、重连读回、自包含结构/实际表示、scope/source 冲突、确认并发、分页、损坏 payload 拒绝和明确删除。
2. Milvus 两项真实集成覆盖 dense/BM25、纯图片空文本、转义过滤在 limit 前执行、scope/space/schema 冲突保留集合、指纹冲突和明确删除。另两项真实回环 HTTP 集成与它们组合运行通过，共四项；S3 用例显式未选中。
3. `examples.persistent_text` 实际生成三个英文 TXT 和一个表格 DOCX，完成 CLIP→PostgreSQL/Milvus→dense/keyword/hybrid。三模式均返回预期猫资料，来源过滤有效；重试保持同一 manifest。
4. 在 `phase-b-d4d059fb5dbe` 范围完成真正的 PostgreSQL/Milvus 容器重启，新进程验证全部 manifest/unit/representation/record 指纹与三模式通过。Milvus healthz/Loaded 先于分片可服务的问题由实际查询就绪探针解决，503 只在配置超时内等待。

该次服务重启发生在最后的表示身份完整性修订之前。修订后使用新 scope `phase-b-24f96d492310` 重新入库与新进程重连验证，最终表示资产守卫加强后也已复验；旧测试范围保留，不覆盖或迁移。再次重启专用测试容器被自动审批阻止，不能把重连当作第二次服务重启。

完整静态检查、单元/覆盖率、提交钩子和仅核心 wheel 安装记录统一见 [交付验证](validation.md)。后续 C 的公开 AWS S3 只读已验证，MinerU 部署待验收；这些单独记录，不计入本阶段文本闭环通过项。

## 保证边界

只承诺 PostgreSQL 内部事务，不承诺跨存储原子提交。真实服务重启可读不等于断电耐久。默认本地资产目录须由调用方持久保存和共享；实例不会关闭注入的客户端，不会自动执行资料更新/激活、后台 GC 或清理旧 scope。
