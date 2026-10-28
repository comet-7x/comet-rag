# 阶段 E 可选检索策略

2026-10-29：父子内容、重排、上下文选择和表示策略已实现并完成真实模型小样本比较。默认仍为 flat，不启用重排或父扩展。

## 父子内容与上下文

`ParentChildIndexBuilder` 使用明确 parent/child flat builders。每个子块的全部来源范围必须由唯一父块完整包含；跨界和重叠歧义明确失败。父块保存完整内容但不产生向量；子块保存实际 `parent_unit_id`，用户 metadata 原样保留，不写入会覆盖业务值的角色标记。

命中 child 与返回 parent context 分开。读取父内容时核对请求 key、对象 unit_id、scope/profile/source/document，不因预算为零省略一致性校验。父上下文稳定去重，预算按 `text_view` 的 Python 字符计数，只选择完整单元，不截断表格或资产。纯图片文字长度为零；这不是 token 或图像字节预算，需要调用方另外约束媒体上下文。

## 重排

`AsyncLocalReranker` 实际使用本地单 logit cross encoder，完整候选先检查 token 预算，再分批评分；不截断输入，不补评分失败的尾部。保存 model/revision/score_kind、完整候选评分和各命中 rerank score，raw dense/BM25 与 RRF 分数仍分开。

Pipeline 默认重排失败退回原融合顺序并记录 `rerank_failed:...`；strict 模式抛错。取消始终传播。内容/实际表示缺失、映射 ID 不符或跨快照，在进入重排前失败，不能被可用性降级隐藏。成功只从完整评分集合取 top_k，不补未评分候选。

`recall_limit` 是每通道 records 上限，`rerank_limit` 是评分候选数，`top_k` 是返回命中数，`context_budget` 是扩展文字字符预算，互不替代。上游多表示占用 recall 预算，unit 去重后不足如实返回。

25 项可选检索边界测试覆盖真实跨界/重叠反例、无向量父持久、metadata 保留、父一致性/预算去重、完整评分/strict/fallback、必要内容错误和活动任务取消。模型适配器另有 10 项注入模型测试，不替代实际推理。

## 真实比较与决定

实际运行 CLIP + `cross-encoder/ms-marco-MiniLM-L6-v2`，8 条预先定义文字查询、12 条文档及两个真实图片。最终报告 `tmp/eval-phase-e-final.json` 保存模型文件 SHA、配置、标签、原始结果和成本。数据与方法、重现命令见 [evals](../evals/README.md)。

Flat 的 Hit@1 为 1.000；parent-child 为 0.875，重排后为 1.000。父子方案向量从 12 增至 33、文字保存量翻倍。Flat 本来已全部命中，当前样本不支持父子方案作为默认。

重排把父子方案的一次改签/退款首位混淆纠正，但八次查询总耗时从约 0.121 秒升至 0.413 秒。样本过小，不作生产延迟或领域质量承诺。

text/media/separate 表示在两张图片上能工作。joint 实际调用 CLIP 后明确 unsupported；本地 cross encoder 对图片候选也明确 unsupported。没有向量平均、OCR 或描述生成来伪装联合模型。更多表示不自动等于更高检索质量。
