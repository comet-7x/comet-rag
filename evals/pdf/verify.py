from __future__ import annotations

import argparse
import json
from hashlib import sha256
from importlib import import_module
from importlib.metadata import version
from pathlib import Path

DATA = Path(__file__).parent


def verify(pdf_dir: Path, *, data: Path = DATA) -> None:
    """独立文档工具环境运行；库和常规 CI 无需安装 PDF 提取依赖。"""
    # 可选提取器只在独立工具进程加载；不要求该进程安装服务/模型依赖。
    reader_type = import_module("pypdf").PdfReader
    snapshot = json.loads((data / "pages.json").read_text(encoding="utf-8"))
    if type(snapshot.get("version")) is not int or snapshot["version"] != 1:
        raise ValueError("未知的 PDF 快照版本")
    if not isinstance(snapshot.get("samples"), list) or not snapshot["samples"]:
        raise ValueError("PDF 来源核验不能使用空样本清单")
    if not snapshot["extractor"].startswith(f"pypdf {version('pypdf')} "):
        raise ValueError("提取器版本与冻结快照不一致")
    for sample in snapshot["samples"]:
        if not isinstance(sample.get("pages"), list) or not sample["pages"]:
            raise ValueError("PDF 来源核验不能使用空页清单")
        source_id = sample["source"]["id"]
        if not isinstance(source_id, str) or not source_id.isalnum():
            raise ValueError("快照 source id 不可用于文件名")
        path = pdf_dir / f"{source_id}.pdf"
        raw = path.read_bytes()
        if (
            len(raw) != sample["pdf_bytes"]
            or sha256(raw).hexdigest() != sample["pdf_sha256"]
        ):
            raise ValueError("原 PDF 字节与锁定来源不一致")
        reader = reader_type(path)
        if len(reader.pages) != sample["total_pages"]:
            raise ValueError("原 PDF 页数不一致")
        previous = 0
        for page in sample["pages"]:
            number = page["page_number"]
            if type(number) is not int or not previous < number <= len(reader.pages):
                raise ValueError("物理页号必须严格递增且位于原 PDF 内")
            if reader.pages[number - 1].extract_text() != page["text"]:
                raise ValueError("物理页提取文本与冻结快照不一致")
            previous = number
    print("Original PDF bytes, physical pages and frozen extraction verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="核验真实 PDF 来源及页快照，不访问网络"
    )
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=DATA)
    args = parser.parse_args()
    verify(args.pdf_dir, data=args.data)
