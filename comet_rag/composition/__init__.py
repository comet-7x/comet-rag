"""组合根：把配置变成一整套装配好的资源。

## 为什么它单独成包

这一层依赖**所有人** —— 它要认识每一个具体实现才能选出该用哪个。而
`core/` 恰恰相反：那是零依赖的运行期内核（日志、闸门、降级），被各层依赖。
两者方向完全相反，此前挤在同一个 `core/` 包里，于是依赖图上 `core` 的箭头
自相矛盾：既指向 services，又被 services 指向。

拆开之后两个名字各自只有一个含义，`tests/unit/test_layering.py` 也才有条
能写清楚的规则可以守（`core/` 不得 import 本项目任何其他包）。

## 只有入口该 import 它

`api/lifespan.py`、`workers/base.py`、`cli.py` —— 进程启动时装配一次。
业务代码从 `Context` 取依赖，绝不自己 new，否则连接池复用与优雅关停都无从谈起。
"""

from .bootstrap import build_context
from .context import Context, wire_runners

__all__ = ["Context", "build_context", "wire_runners"]
