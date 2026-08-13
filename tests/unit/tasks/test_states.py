"""状态机全矩阵测试。

`states.py` 是纯函数、零依赖，却是全包 bug 后果最严重的地方 ——
「已取消的任务又变成成功了」「失败的任务没有 finished_at」这类问题
只在生产偶发，事后极难复现。投入产出比最高的测试。

**期望矩阵是手写的，刻意不从 `_ALLOWED` 推导。**
写成 `assert can_transition(a, b) == (b in _ALLOWED[a])` 是拿实现验证实现，
恒真、一无所证；改坏 `_ALLOWED` 时它会跟着一起变，永远不会红。
下面这张表是独立的第二份真相，改动状态机必须同时改它 —— 那正是我们想要的摩擦。
"""

from __future__ import annotations

import pytest

from comet_rag.tasks import TaskStatus, assert_transition, can_transition
from comet_rag.tasks.states import InvalidTransition

# ── 期望矩阵（手写，与实现无关）────────────────────────────────────────────
#
#   行 = 起点，列 = 终点，yes/no = 是否合法，`-` = 自迁移（恒合法）
#
#   设计意图速览：
#     · PENDING 不能直接到 SUCCEEDED —— 没跑过的任务不该算成功
#     · RUNNING 不能直接到 CANCELLED —— 取消是协作式的，那一刻 runner 还在跑，
#       必须先进 CANCELLING，等它走到 checkpoint 自己退出
#     · RUNNING 可以回 PENDING —— 可重试失败重排队 / 租约过期回收
#     · CANCELLING 可以到 SUCCEEDED/FAILED —— runner 可能在收到取消前就跑完了
#     · FAILED 可以回 PENDING —— is_terminal 的唯一例外，需调用方显式 retry
#     · SUCCEEDED / CANCELLED 是死路
#
_MATRIX = """
              pending  running  cancelling  succeeded  failed  cancelled
pending          -       yes        no          no       yes      yes
running         yes       -         yes        yes       yes       no
cancelling       no      no          -         yes       yes      yes
succeeded        no      no         no           -        no       no
failed          yes      no         no          no         -       no
cancelled        no      no         no          no        no        -
"""


def _parse_matrix() -> dict[tuple[TaskStatus, TaskStatus], bool | None]:
    """`None` 表示自迁移格（对角线），由单独的用例覆盖。"""
    lines = [ln for ln in _MATRIX.strip().splitlines() if ln.strip()]
    cols = [TaskStatus(c) for c in lines[0].split()]
    table: dict[tuple[TaskStatus, TaskStatus], bool | None] = {}
    for line in lines[1:]:
        row_name, *cells = line.split()
        frm = TaskStatus(row_name)
        assert len(cells) == len(cols), f"{row_name} 行的格子数与表头不符"
        for to, cell in zip(cols, cells, strict=True):
            table[(frm, to)] = None if cell == "-" else cell == "yes"
    return table


EXPECTED = _parse_matrix()
OFF_DIAGONAL = [(f, t) for (f, t), v in EXPECTED.items() if v is not None]


# ── 矩阵自检 ───────────────────────────────────────────────────────────────


def test_matrix_covers_every_status_pair() -> None:
    """新增状态值时，这里会先红 —— 提醒你把矩阵补全，而不是留个空洞。"""
    statuses = list(TaskStatus)
    assert len(EXPECTED) == len(statuses) ** 2, (
        f"矩阵有 {len(EXPECTED)} 格，但 {len(statuses)} 个状态需要 "
        f"{len(statuses) ** 2} 格"
    )
    for frm in statuses:
        for to in statuses:
            assert (frm, to) in EXPECTED, f"矩阵缺少 {frm.value} → {to.value}"


def test_diagonal_is_self_transitions() -> None:
    for status in TaskStatus:
        assert EXPECTED[(status, status)] is None


# ── 全矩阵 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("frm", "to"), OFF_DIAGONAL, ids=[f"{f.value}->{t.value}" for f, t in OFF_DIAGONAL]
)
def test_can_transition_matches_matrix(frm: TaskStatus, to: TaskStatus) -> None:
    expected = EXPECTED[(frm, to)]
    assert can_transition(frm, to) is expected, (
        f"{frm.value} → {to.value} 期望 {'合法' if expected else '非法'}，实际相反"
    )


@pytest.mark.parametrize(
    ("frm", "to"), OFF_DIAGONAL, ids=[f"{f.value}->{t.value}" for f, t in OFF_DIAGONAL]
)
def test_assert_transition_agrees_with_can_transition(
    frm: TaskStatus, to: TaskStatus
) -> None:
    """守卫函数与判定函数不得各说各话。"""
    if EXPECTED[(frm, to)]:
        assert_transition(frm, to)  # 不抛即通过
    else:
        with pytest.raises(InvalidTransition) as exc:
            assert_transition(frm, to)
        assert exc.value.frm is frm
        assert exc.value.to is to


@pytest.mark.parametrize("status", list(TaskStatus), ids=lambda s: s.value)
def test_self_transition_always_allowed(status: TaskStatus) -> None:
    """幂等写入不该被守卫拦住：重复投递、重放事件都可能触发同状态写入。"""
    assert can_transition(status, status) is True
    assert_transition(status, status)


# ── 不变式 ─────────────────────────────────────────────────────────────────


def test_terminal_states_have_no_exits_except_failed() -> None:
    """SUCCEEDED / CANCELLED 是死路；FAILED 是唯一可被显式 retry 重开的终态。"""
    for status in TaskStatus:
        if not status.is_terminal:
            continue
        exits = {
            to for to in TaskStatus if to is not status and can_transition(status, to)
        }
        if status is TaskStatus.FAILED:
            assert exits == {TaskStatus.PENDING}
        else:
            assert exits == set(), f"{status.value} 是终态却能迁往 {exits}"


def test_every_state_is_reachable_from_pending() -> None:
    """没有孤岛状态。有的话说明要么是死代码，要么是漏了一条迁移。"""
    seen = {TaskStatus.PENDING}
    frontier = [TaskStatus.PENDING]
    while frontier:
        frm = frontier.pop()
        for to in TaskStatus:
            if to not in seen and can_transition(frm, to):
                seen.add(to)
                frontier.append(to)

    assert seen == set(TaskStatus), f"无法从 PENDING 到达：{set(TaskStatus) - seen}"


def test_non_terminal_states_can_all_reach_a_terminal() -> None:
    """任何在途状态都必须有出路，否则任务会永久卡死。"""
    terminals = {s for s in TaskStatus if s.is_terminal}
    for status in TaskStatus:
        if status.is_terminal:
            continue
        seen, frontier = {status}, [status]
        while frontier:
            frm = frontier.pop()
            for to in TaskStatus:
                if to not in seen and can_transition(frm, to):
                    seen.add(to)
                    frontier.append(to)
        assert seen & terminals, f"{status.value} 无法到达任何终态"


def test_is_terminal_and_is_active_are_disjoint():
    for status in TaskStatus:
        assert not (status.is_terminal and status.is_active), (
            f"{status.value} 不能既是终态又占着执行槽"
        )


def test_active_states_are_exactly_running_and_cancelling() -> None:
    """is_active 决定 delete 是否放行、sweep_stale 扫哪些任务，值错了后果直接。"""
    assert {s for s in TaskStatus if s.is_active} == {
        TaskStatus.RUNNING,
        TaskStatus.CANCELLING,
    }


def test_terminal_states_are_exactly_the_three() -> None:
    assert {s for s in TaskStatus if s.is_terminal} == {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
