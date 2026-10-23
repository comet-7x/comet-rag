# 查询 deadline 执行机制

`services/query_deadline.py` 提供单调时钟绝对预算。调用方在一次操作入口创建
QueryDeadline，多个阶段调用同一实例的 run，共同消耗剩余时间。

run 接收协程工厂；预算已经耗尽时不创建协程或启动 IO。每阶段工作有自己的受管 Task，
超时后仅取消一次并等待排干。调用方重复取消不打断清理，取消期间暴露的致命异常继续传播。
吞取消或阻塞事件循环产生的迟到成功不能作为有效结果。

取消清理可能超过预算；同步线程无法强杀，应由线程桥等待工作结束后释放资源。
QueryDeadline 不决定业务降级、HTTP 状态或后端能力，这些由调用层负责。

执行机制使用标准库 asyncio.wait（超时不自动取消工作）和 shield（保护排干）。
依据：[Python 3.12 asyncio 的等待与取消语义](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.wait)。

独立回归位于 `tests/unit/services/test_query_deadline_execution.py`，覆盖预算过期不启动工作、
迟到结果、吞取消成功与清理致命错误。
