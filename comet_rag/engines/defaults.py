"""纯库模式的资源预算默认值；服务模式可由配置覆盖。"""

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
