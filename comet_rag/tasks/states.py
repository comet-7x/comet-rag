"""任务状态机：唯一的一张合法迁移表。

为什么值得单独一个模块：状态字段一旦允许被任意 `update(status=...)` 改写，
「已取消的任务又变成成功了」「失败的任务没有 finished_at」这类 bug 会散落在
各个 runner 里，且只在生产环境偶发。把规则收敛成一张表 + 一个守卫函数，
所有写入都必须过 `TaskStore.transition()`。

    PENDING ──► RUNNING ──┬─► SUCCEEDED
       ▲         │        ├─► FAILED ──► PENDING（显式 retry）
       │         │        └─► CANCELLING ──► CANCELLED
       └─────────┴ 可重排队（可重试失败 / 租约过期回收）

注意 PENDING 不能直接跳到 SUCCEEDED —— 没跑过的任务不该算成功。
"""

from __future__ import annotations

from .models import TaskStatus as S

_ALLOWED: dict[S, frozenset[S]] = {
    # 排队中：可开跑、可直接取消；参数非法等前置校验失败可直接判死
    S.PENDING: frozenset({S.RUNNING, S.CANCELLED, S.FAILED}),
    # 执行中：正常收尾、受理取消、或（可重试失败 / 租约回收）退回排队
    S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLING, S.PENDING}),
    # 取消中：runner 可能在收到取消前就已经跑完了，所以成功/失败也是合法落点
    S.CANCELLING: frozenset({S.CANCELLED, S.SUCCEEDED, S.FAILED}),
    # 失败可被**显式** retry 重新打开（这是 is_terminal 唯一的例外，需调用方主动发起）
    S.FAILED: frozenset({S.PENDING}),
    S.SUCCEEDED: frozenset(),
    S.CANCELLED: frozenset(),
}


class InvalidTransition(RuntimeError):
    def __init__(self, frm: S, to: S) -> None:
        super().__init__(f"非法状态迁移：{frm.value} → {to.value}")
        self.frm, self.to = frm, to


def can_transition(frm: S, to: S) -> bool:
    return frm is to or to in _ALLOWED[frm]


def assert_transition(frm: S, to: S) -> None:
    if not can_transition(frm, to):
        raise InvalidTransition(frm, to)
