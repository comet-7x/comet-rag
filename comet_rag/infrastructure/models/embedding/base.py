"""Embedding 模型的稳定调用契约。

## 闸门为什么放在基类而不是包装器上

spec §7 写着"对模型服务的调用一律经过并发闸门，**不允许裸调**"。
用装饰器包一层也能做到，但那只是**约定**：谁直接拿着 `Qwen3VLEmbeddingModel`
调一下，就绕过去了，而且不会有任何报错。

所以这里使用模板方法：`aembed()` 是带闸门的稳定入口，子类只实现
`_aembed()`。`@final` 让类型检查器能够拦截误覆写；运行时的装配守卫与测试
则负责确认生产实例确实绑定了闸门。

同步的 `embed()` 不经闸门：`Gate` 基于 `asyncio.Semaphore`，管不了线程池。
那条路是"当库用"的场景，服务端全链路异步不会走到。见 `core/concurrency.py`。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, final

if TYPE_CHECKING:
    from comet_rag.core.concurrency import Gate


DEFAULT_MODEL_BATCH_CONCURRENCY = 16


class BaseEmbeddingModel(ABC):
    #: 进程级并发闸门，由组合根注入。None = 不限流（当库用、单测）。
    _gate: Gate | None = None

    def bind_gate(self, gate: Gate | None) -> None:
        """挂上闸门。由 `core/bootstrap.py` 调用，**同一个 Gate 实例**
        也会挂给 reranker —— 它们抢的是同一块 GPU，分开限流等于没限。"""
        self._gate = gate

    # ── 对外入口（终态方法，闸门在这里）─────────────────────────────────────

    @final
    async def aembed(self, data: Any, **kwargs: Any) -> Any:
        """向量化一条。**不要覆写它**，要改行为请实现 `_aembed`。"""
        if self._gate is None:
            return await self._aembed(data, **kwargs)
        async with self._gate:
            return await self._aembed(data, **kwargs)

    @abstractmethod
    async def _aembed(self, data: Any, /, **kwargs: Any) -> Any:
        """真正打网络的那一步。子类实现这个，闸门由基类负责。

        第一个参数声明成**仅位置**（`/`）：基类只会位置传参，而各实现给它起了
        贴合自己领域的名字（`text` / `embedding_data`）。不加这个斜杠的话，
        名字不一致就是一处 LSP 违背 —— 谁按基类签名写 `_aembed(data=…)` 就会炸。
        """

    @abstractmethod
    def embed(self, *args: Any, **kwargs: Any) -> Any:
        """同步版。**不经闸门**（见模块文档），仅供"当库用"的场景。"""

    # ── 批量 ───────────────────────────────────────────────────────────────

    def batch_embed(
        self,
        embedding_data_list: list[Any],
        *,
        max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
        **kwargs: Any,
    ) -> list[Any]:
        """在线程池中并发向量化，返回顺序与输入一致。

        `max_concurrency` 是单次批处理的扇出上限，不是模型服务的全局并发
        上限。同步接口无法使用基于 `asyncio.Semaphore` 的进程级闸门。
        """
        self._validate_max_concurrency(max_concurrency)
        if not embedding_data_list:
            return []

        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        max_workers = min(max_concurrency, len(embedding_data_list))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(self.embed, data, **kwargs)
                for data in embedding_data_list
            ]
            return [future.result() for future in futures]

    async def abatch_embed(
        self,
        embedding_data_list: list[Any],
        *,
        max_concurrency: int = DEFAULT_MODEL_BATCH_CONCURRENCY,
        **kwargs: Any,
    ) -> list[Any]:
        """并发向量化一批。

        `max_concurrency` 只约束**这一次调用**的扇出宽度（限制同时活着的
        协程数，从而限制内存）。**它不是对模型服务的并发上限** —— 那个上限
        由进程级的 `_gate` 负责。

        两者的区别曾经是个真实的坑：只有前者时，32 个任务各开 4 路扇出，
        对模型服务的实际并发是 128，而配置里写的是 4（实测）。
        """
        self._validate_max_concurrency(max_concurrency)
        if not embedding_data_list:
            return []

        semaphore = asyncio.Semaphore(min(max_concurrency, len(embedding_data_list)))

        async def _limited(data: Any) -> Any:
            async with semaphore:
                return await self.aembed(data, **kwargs)

        return await asyncio.gather(*[_limited(d) for d in embedding_data_list])

    @staticmethod
    def _validate_max_concurrency(max_concurrency: int) -> None:
        if max_concurrency <= 0:
            raise ValueError(f"max_concurrency 必须大于 0，收到 {max_concurrency}")

    async def aclose(self) -> None:
        """释放模型持有的客户端；无资源的实现可以沿用默认空操作。"""
        return None
