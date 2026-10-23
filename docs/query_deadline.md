# 查询 deadline 与降级

`RetrievalService.search()` 为每次调用创建一个单调时钟绝对 deadline。默认工作预算
为 30 秒，部署配置为 `retrieval.timeout_seconds`；直接使用服务时可传入同名构造参数。
预算必须是有限正数，不能用 null/0 关闭。不同查询不共享截止时刻。

## 预算范围

| 阶段 | 计入同一次预算的工作 | 预算耗尽时 |
|---|---|---|
| KB 检查 | 仓储连接池等待、模型身份检查 | 抛出 QueryDeadlineExceeded；HTTP 504 |
| dense 召回 | embedding 闸门排队、SDK 连接池等待、模型请求、向量查询、两轮 revision 召回及 active 判活 | 单路请求失败；hybrid 可保留已完成的另一通道 |
| keyword 召回 | 关键词查询、连接池等待、revision 召回及 active 判活 | 同上 |
| 重排 | 模型闸门排队、供应商调用 | 保留原召回/融合顺序与分数，reranked=false |
| 父块回填 | DocumentStore 连接池等待、父块读取 | 保留已判活的 child；若重排已成功则保留重排分数 |

所有阶段使用剩余时间，dense 与 keyword 并行时也共享同一个截止时刻。
不重置阶段计时，不按阶段额外叠加预算，也不预留固定比例给重排或父块。
这意味着召回可能消耗全部预算；已有安全候选仍可返回，但后续可选 IO 不会再启动。
CPU 上的融合、校验和响应组装没有强制抢占能力；该预算不等于 HTTP 总耗时保证。

服务入口之前的 HTTP 接入、请求体读取与校验、反向代理排队，以及响应发送不在此
预算内。若需要覆盖这些阶段，需在代理/服务器另设请求期限，不能把服务预算冒充为
外部总 SLA。

## 结果与错误

- hybrid 只有完成全部召回和判活的通道才可参与结果。某路超时会记录该路
  `reason=deadline_exceeded`，实际 mode/channels 指向成功通道。成功返回空列表也属于
  有效通道结果，不等于通道失败。
- hybrid 两路均失败且至少一路耗尽共享预算时抛出 QueryDeadlineExceeded，HTTP 504；
  两路仅发生普通可恢复故障时仍为 HybridRecallFailed，HTTP 503。
- 重排和父块超时返回已有候选，HTTP 200，degradations 中相应阶段记录
  `reason=deadline_exceeded`、`error_type=QueryDeadlineExceeded`。未配置或显式关闭
  重排不制造超时诊断；没有 parent_id 时不发起父块读取。
- active 判活没有完成时，候选不能作为回退结果。判活存储本身报错仍是不可降级的
  RevisionVisibilityError；schema、参数和编程错误也沿用原有拒绝规则。
- 模型闸门的席位上限和 acquire_timeout 继续独立有效：先触发的拒绝仍为 Overloaded
  （必需召回为 HTTP 429，重排按原规则降级）；共享 deadline 先触发则按本页处理。
- SDK/网络自己的 TimeoutError 不会被改称共享预算耗尽，沿用原有错误或降级规则。
  外部调用方的 CancelledError 原样传播，不能被转换成成功、504 或质量降级。

## 取消与资源所有权

预算耗尽只发起一次取消，然后等待该阶段及其拥有的子任务清理完成。hybrid 保留已有的
取消/排干边界；重复取消不会打断资源清理。若调用方在超时清理期间取消，最终仍传播
调用方取消。取消时暴露的致命异常不伪装成通道抖动。

同步线程不能强杀：线程桥必须等线程结束才释放闸门和资源。因此清理时间可能超过
deadline；不释放仍在使用的资源、不遗留脱离查询生命周期的工作。协程吞掉取消后
返回的迟到成功也不会被当作有效结果。

实现采用 Python 标准库 asyncio.wait（超时不自动取消子任务）以及 shield 包住排干，
而不是在整个 search 外套一个会抹掉 hybrid 已成功结果的超时。
依据：[Python 3.12 asyncio 的等待与取消语义](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.wait)。

## 回归入口

`tests/unit/services/test_query_deadline.py` 覆盖共享预算、排队、hybrid 单路存活、
重排和父块回退、判活拒绝、调用方取消与线程闸门所有权。
执行原语的迟到成功、吞取消及清理致命错误守卫位于
`tests/unit/services/test_query_deadline_execution.py`。
`tests/e2e/test_ingest_search.py::test_configured_query_budget_covers_model_queue_and_returns_504`
经过真实应用装配和模型闸门验证配置接线及 HTTP 504，不访问外部模型。
