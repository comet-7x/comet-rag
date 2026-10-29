from __future__ import annotations

import os
import re
import subprocess  # nosec B404
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("branch", "accepted"),
    [
        ("feature/$(touch${IFS}injected)", True),
        ("refactor/v2", True),
        ("invalid", False),
    ],
)
def test_branch_check_treats_branch_name_as_data(
    tmp_path: Path, branch: str, accepted: bool
) -> None:
    workflow = (ROOT / ".github/workflows/commit_message_check.yml").read_text()
    step = workflow.split("- name: Run Commitizen Branch Check\n", 1)[1]
    script = step.split("run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in script.splitlines())
    script = script.replace("${{ github.head_ref }}", branch)
    environment = os.environ | {"BRANCH_NAME": branch}
    # 执行仓库自己的校验脚本，用无害临时文件证明输入没有被执行。
    result = subprocess.run(  # noqa: S603  # nosec B603
        ["/bin/bash", "--noprofile", "--norc", "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not (tmp_path / "injected").exists()
    assert (result.returncode == 0) is accepted
    assert re.search(r"BRANCH_NAME:.*\$\{\{ github.head_ref }}", step)
