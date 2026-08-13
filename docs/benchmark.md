# 性能基线

> spec S4-6：`tests/benchmark/` 能产出当前基线；涉及性能的 PR 须附前后对比。

## 先说这些数字**不是**什么

**它们不回答"这个服务能扛多少 QPS"。** 基线跑在替身模型上（瞬间返回），
所以耗时里剩下的全是本项目自己的部分：任务状态机的每次落库、向量库读写、
序列化、路由与依赖注入。真实吞吐取决于 GPU、模型和文档，跟本项目关系不大。

把两者混为一谈是性能数字最常见的误读方式。**这份基线的用途只有一个：
发现回归。** 谁给每个 chunk 多加了一次数据库往返，这里立刻会看出来。

同理，**跨机器比数字没有意义**。报告里一并存了 CPU、平台与 git 版本，
对比时先确认这三项一致。

## 怎么跑

```bash
uv run pytest -m benchmark                          # 产出 bench-report.json
uv run pytest -m benchmark --bench-out before.json  # 改动前存一份
# ……改代码……
uv run pytest -m benchmark --bench-baseline before.json   # 打印增减
```

对比输出会给每项标出方向（耗时越低越好、吞吐越高越好），变化 <5% 记 ✅。

`bench-report.json` 是生成物，不进版本库；要留档就存进 PR 描述或 CI 产物。

## 当前基线

采集环境：Python 3.12 / Linux x86_64 / 全内存后端（`memory` vector store +
`memory` task store + `inprocess` executor）。文档 200 段，`embed_batch_size=32`、
`max_concurrency=16`。

| 用例 | 指标 | P50 | P95 | 说明 |
|---|---|---:|---:|---|
| 单文档入库 | `ingest_e2e` | **19.2 ms** | 23.1 ms | 200 段，含轮询间隔 |
| 批量入库 | `throughput_docs` | **85 doc/s** | — | 20 份并发 |
| 批量入库 | `throughput_chunks` | **17 043 chunk/s** | — | 同上 |
| 检索（无重排） | `search_no_rerank` | **3.21 ms** | 3.39 ms | 500 段库，top_k=5 |
| 检索（含重排） | `search_rerank` | **3.22 ms** | 3.46 ms | 替身重排，量的是框架开销 |
| 检索 top_k=50 | `search_top_k_50` | **3.74 ms** | 4.10 ms | 降级 L2 的收益来源 |
| 并发重叠 | `overlap_speedup` | **10.95×** | — | 见下 |

### 关于 `overlap_speedup`

这是唯一**带断言**的基准，因为它的判据是结构性的而非计时性的。

给替身模型加 5 ms 固定延迟，200 段若逐条串行光等待就是 1000 ms；
窗口化并发下实测 92 ms（≈ 70 ms 等待 + 21 ms 框架），加速比 10.95×。
断言阈值取 `serial / 4`，离实测值很远，换机器也不会假红。

守的是 T9 修好的那件事（修复前 `astream_run` 并发峰值恒为 1）。
反向验证：把 `_index` 改回逐条 `await aembed()`，实测 1097 ms，用例立刻变红。

### 关于重排的耗时

表里"含重排"与"无重排"几乎一样（3.22 vs 3.21 ms），因为替身重排是纯计算。
**真实模型下这两行会差一到两个数量级** —— 交叉编码器要给几十个候选逐个打分。
分级降级（S4-5）第一步就砍它，理由正在于此；这张表只能证明框架侧没有额外浪费。

## 采集方式的取舍

**没有用 pytest-benchmark。** 它面向同步微基准，会对同一个函数反复校准重跑，
而这里每次测量都带状态（入库会写库、建 collection），重跑会互相污染。
它报的也是 mean/median/stddev，没有 P95/P99 —— 而验收标准要的恰恰是后者。
自己写的采集器见 `tests/benchmark/conftest.py`。

**分位数用"最近秩"而不是插值**：样本量小时插值出来的 P99 更像数学产物，
而"排序后第几个"至少是一次真实发生过的耗时。

**除 `overlap_speedup` 外不对耗时做断言**：机器一换数字就变，那种用例只会
训练出"红了就重跑"的习惯。回归靠 `--bench-baseline` 的对比发现，是人看的，
不是 CI 判的。

## 一个踩过的坑

`tests/e2e/test_ingest_search.py::_use_stub_loader` 会**重新注册** runner，
从而覆盖掉 `create_app(pipeline_config=...)` 装配的那一份。基准最初没意识到
这点，以为自己配的是 `32/16`，实际跑的是该函数硬编码的 `2/4` ——
`overlap_speedup` 因此只有 1.74×，怎么算都对不上。

现在 `_use_stub_loader` 接受显式的 `config` 参数，基准必须传自己的。
**基准跑错了配置，比没有基准更糟**：它会给出一个看似精确、实则量错了东西的数字。
