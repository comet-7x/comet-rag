# 入库写入保护与失败边界

flat入库默认沿用按source/ordinal稳定ID，不是原子换版。现在通过既有TaskStore的
唯一幂等键与CAS按(kb_id,source_id)排他，保护从旧块计数、全部upsert窗口到尾部删除。
其他来源/KB不共用该围栏；并发同源任务明确失败，不同时报告成功后留下混合版本。

围栏控制记录kind为flat-source-write，不进入队列，创建后封存为CANCELLED，
writer保存task_id和独立invocation。正常RPC返回后才清除本地不确定状态；超时、
取消或异常不能证明远端没写入，保留writer及控制记录，不因租约/年龄自动接管。
普通TaskService的force/TTL删除不能清掉控制记录，worker补投排除该kind。

调用模型/写库前确定性失败可释放围栏；但已成功写入的其他窗口不会回滚。多窗口
失败仍可能混合新旧块，完整换版应使用新命名空间revision路径。所有写者必须共享
TaskStore并参与该协议；尚在运行的旧代码或直接SDK写入不受保护。

遇到未知RPC，先暂停该来源新写、按记录核实远端和内容并独立归档恢复决定。本轮
没有提供自动清锁、年龄回收或强制重试后门。不得删除唯一身份后假设重跑安全。

空白提取在保留到Task前拒绝；正常分块复用既有原文覆盖检查。绕过分块的恢复任务
若带空draft，在indexing计数/删除前拒绝。revision准备不允许零向量子块覆盖active。
这些错误均保留旧版本或原active指针，不把“没有正文”解释为“成功删除文档”。

共享IngestRequest由API/服务入口复用，metadata必须为有限JSON对象，UTF8序列化
不超过64KiB；每块复制metadata的放大仍取决于块数。此处不限制HTTP解析器最初接收
的请求体，公开部署还需代理/服务器的有界请求接收。降级阈值在配置预检要求有限
正数、失败率≤1且级别非递减，非法负数不会静默变成永久拒写。

回归覆盖同源3→5/1块竞态、空提取/空chunker/恢复空draft、空revision、未知RPC、
CAS提交回包丢失和真实PG跨连接竞争。范围与最终验证见
[收口报告](evaluations/mineru_wiring_20261028.md)。
