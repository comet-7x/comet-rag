# 检索策略小样本评估

`retrieval_strategies.py` 实际运行本地 CLIP 和单 logit cross encoder，比较 flat / parent-child、重排开关和文本/图片表示。没有模型服务、LLM 裁判或回答生成。

```bash
python -m evals.retrieval_strategies \
  --clip-model-dir /absolute/path/to/local-clip \
  --reranker-model-dir /absolute/path/to/local-reranker \
  --image-dir /absolute/path/to/cat-and-dog \
  --report-path tmp/retrieval-comparison.json
```

模型版本与图片来源见 [examples](../examples/README.md)。运行不联网；模型目录只加载本地 safetensors。依赖为 `local-models`，不需要 PostgreSQL 或 Milvus。报告保存全部输入标签、文档/空间/配置身份、文件 SHA-256、模型原始分数、评分候选、上下文和计时。

## 方法

固定 12 条作者编写的短文档、8 条独立 query/source 标签，未按分数调优。flat 使用 320 字符切分，parent-child 使用 320 字符父块和 96 字符子块。检索全部实际表示，重排全部命中候选；同来源按第一次命中去重，再计算 MRR、Hit@1/3、Recall@3。parent-child 扩展显式父内容，600 字符预算只选择完整上下文。

图片部分使用两个真实对象，分别比较人工文字标签、实际图片和独立文字/图片表示。图片查询为同图身份检查；文字标签由人预先提供，不是 OCR 或自动图像描述。联合图文和图片重排确实调用适配器验证拒绝，不以向量平均代替联合模型。

## 2026-10-29 本机结果

最终运行记录为 `tmp/eval-phase-e-final.json`，在身份完整性修复后生成。CPU，torch threads=2，各条件单次运行：

| 策略 | 重排 | MRR | Hit@1 | Recall@3 | 8 次查询总耗时 |
|---|---|---|---|---|---|
| Flat | 关 | 1.0000 | 1.000 | 1.000 | 0.0742 秒 |
| Flat | 开 | 1.0000 | 1.000 | 1.000 | 0.3528 秒 |
| Parent-child | 关 | 0.9375 | 0.875 | 1.000 | 0.1212 秒 |
| Parent-child | 开 | 1.0000 | 1.000 | 1.000 | 0.4133 秒 |

唯一首位错误发生在 parent-child 的改签查询：退款说明排在改签说明之前，重排纠正了它。Flat 在同组样本已经全部命中，因此该实验没有证明父子切分比 flat 更好。

Flat 保存 12 units / 12 representations；parent-child 保存 12 父 + 33 子 / 33 representations。理论 float32 向量 payload 从 24,576 增至 67,584 字节（2.75 倍），保存文本从 2,366 增至 4,732 字符。父子上下文和重排保持显式选择，不设为默认。

两图片的 text/media/separate 都能命中；独立表示把表示数从 2 增至 4。当前 CLIP 对联合图文返回 unsupported；当前 cross encoder 对图片候选返回 unsupported。Pipeline 默认重排失败退回原排名并记录诊断，`strict_rerank=True` 可使其失败。

逐模块复审在当前 worktree 重新运行同一真实 CLIP/MiniLM 评估，报告为 `tmp/eval-module-review-20261029.json`。使用 Python 3.12.13、torch 2.14.1、transformers 4.57.6、Pillow 13.0.0、CPU threads=2；权重 SHA 与上述固定模型一致。四组文本指标与表中相同，查询总耗时依次为 0.0819 / 0.3823 / 0.1619 / 0.4983 秒。另由独立校验程序从 44 行原始 unit/source 排名与相关标签重新计算逐查询指标和均值，全部一致；text/media/separate 图片组指标均为 1，joint 和图片重排仍明确 unsupported。没有改动样本、标签或默认策略。

这些是小型功能/成本证据，不是领域基准或统计显著结果。没有留出外部测试集、吞吐/并发、峰值 RSS、GPU 或付费成本测量；理论向量字节不等于实际数据库占用。dense top-k 对无关查询仍可能返回结果，拒绝低相关结果需要另外标注和校准。
