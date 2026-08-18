"""engines 的并发默认值 —— **只有这一处**。

## 为什么集中，但**不**统一成一个数

这些数字原本散在三个文件里：loader 是 10、嵌入扇出是 16、管道是 8，
三处都没说明为什么。`base_loader.py` 的注释甚至已经在为自己辩解：

    DEFAULT_MAX_CONCURRENCY is deliberately a safety cap rather than a claim
    about optimal throughput.

它自己都承认这个数字没有依据。

但压成同一个数也是错的：**它们保护的根本不是同一种资源**。加载并发消耗的是
本机文件描述符与对外连接数，嵌入扇出消耗的是模型服务（通常是一块 GPU）的
排队位。两者的合理值差一个数量级，硬拉平只会让"调大加载并发"意外挤掉模型的
名额 —— 与「loader 闸门不该和模型闸门共用」是同一个道理。

所以这里的做法是：集中在一处、**每个数字写明它护住的是什么**，而不是求一致。

## 这些只是"当库用"时的兜底

跑参考服务时，真正生效的数字来自 `config/schemas.py::LimitsConfig`，由
`composition/bootstrap.py` 注入。下面的值只在调用方什么都不说时兜底 ——
所以它们必须**保守**：宁可慢，不可打爆别人的服务。
"""

from __future__ import annotations

#: 单次批量加载的并发上限。护的是**本机文件描述符与对外连接数**。
#:
#: 10 是保守值：默认 fd 上限常见为 1024，而一次加载可能同时持有连接、临时文件
#: 与解压句柄。真要提高吞吐，先确认 ulimit 和对端的速率限制。
DEFAULT_LOADER_CONCURRENCY = 10

#: 单次批量嵌入的扇出宽度。护的是**模型服务**。
#:
#: 8 对应「一块消费级 GPU 上跑一个 vLLM」这个基准场景，与
#: `LimitsConfig.model_concurrency` 的默认值取齐 —— 当库用（没有闸门）时，
#: 它就是唯一的上限，所以不能比闸门宽。
DEFAULT_EMBED_FANOUT = 8

#: 流式管道每次处理多少个 chunk，即**产出粒度**。护的是**内存峰值**。
#:
#: 它不是并发数：越小首字延迟越低、整体吞吐越差，越大则相反。32 是两者之间
#: 一个能用的折中；超大文档靠它把同时在内存中的待处理量限住。
DEFAULT_EMBED_WINDOW = 32

__all__ = [
    "DEFAULT_EMBED_FANOUT",
    "DEFAULT_EMBED_WINDOW",
    "DEFAULT_LOADER_CONCURRENCY",
]
