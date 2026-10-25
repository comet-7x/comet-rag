from __future__ import annotations

import ast
import json
import subprocess
from hashlib import sha256
from importlib.metadata import distribution
from pathlib import Path
from shutil import which
from tempfile import TemporaryDirectory

DATA = Path(__file__).parent


def install(site_packages: Path, version: str) -> None:
    """只修改停止运行的独立 MinerU 环境；先验全部基线，再生成并安装补丁。"""
    lock = json.loads((DATA / "upstream.json").read_text())
    if version != lock["version"]:
        raise ValueError("部署扩展只验证了 MinerU 3.4.5")
    originals = {}
    for name, expected in lock["files"].items():
        path = site_packages / name
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != expected:
            raise ValueError(f"上游源码摘要不匹配，停止安装：{name}")
        originals[name] = raw
    helper = "mineru/cli/comet_page_markdown.py"
    if (site_packages / helper).exists():
        raise ValueError("部署扩展已存在，拒绝覆盖")

    executable = which("patch")
    if executable is None:
        raise RuntimeError("独立部署环境需要系统 patch 工具")
    with TemporaryDirectory(prefix="comet-mineru-patch-") as temporary:
        staging = Path(temporary)
        for name, raw in originals.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        subprocess.run(  # noqa: S603 -- 固定参数、仓库随附补丁，无 shell 或请求输入。
            [executable, "--batch", "--forward", "-p1", "-d", str(staging)],
            input=(DATA / "mineru-3.4.5-page-markdown.patch").read_bytes(),
            check=True,
            capture_output=True,
        )
        updates = {name: (staging / name).read_bytes() for name in originals}
        updates[helper] = (DATA / "page_markdown.py").read_bytes()
        for name, raw in updates.items():
            ast.parse(raw, filename=name)
        try:
            for name, raw in updates.items():
                (site_packages / name).write_bytes(raw)
        except OSError:
            for name, raw in originals.items():
                (site_packages / name).write_bytes(raw)
            (site_packages / helper).unlink(missing_ok=True)
            raise


if __name__ == "__main__":
    mineru = distribution("mineru")
    install(Path(str(mineru.locate_file(""))), mineru.version)
    print("Installed explicit page Markdown extension into independent MinerU 3.4.5")
