from __future__ import annotations

import re
from dataclasses import dataclass

from comet_rag.ports.document import DocumentBlock

_ATX_HEADING = re.compile(
    r"^ {0,3}(?P<marks>#{1,6})(?:[ \t]+(?P<title>.*)|[ \t]*)$"
)
_ATX_CLOSING = re.compile(r"[ \t]+#+[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<rest>.*)$")
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?P<marker>=+|-+)[ \t]*$")


@dataclass(frozen=True, slots=True)
class _Heading:
    start: int
    level: int
    title: str


class MarkdownStructureAnalyzer:
    """识别 Markdown 标题区间；代码围栏内的内容不参与标题层级。"""

    def analyze(self, markdown: str, /) -> tuple[DocumentBlock, ...]:
        if not isinstance(markdown, str):
            raise TypeError("markdown 必须是 str")
        if not markdown:
            return ()

        headings = self._headings(markdown)
        if not headings:
            return (DocumentBlock("section", 0, 0, len(markdown)),)

        ranges: list[tuple[int, int, tuple[str, ...]]] = []
        if headings[0].start > 0:
            ranges.append((0, headings[0].start, ()))

        path: list[str] = []
        for index, heading in enumerate(headings):
            del path[max(heading.level - 1, 0) :]
            path.append(heading.title)
            end = (
                headings[index + 1].start
                if index + 1 < len(headings)
                else len(markdown)
            )
            ranges.append((heading.start, end, tuple(path)))

        return tuple(
            DocumentBlock(
                kind="section",
                ordinal=ordinal,
                start_char=start,
                end_char=end,
                heading_path=heading_path,
            )
            for ordinal, (start, end, heading_path) in enumerate(ranges)
            if start < end
        )

    @staticmethod
    def _headings(markdown: str) -> list[_Heading]:
        lines = markdown.splitlines(keepends=True)
        starts: list[int] = []
        cursor = 0
        for line in lines:
            starts.append(cursor)
            cursor += len(line)

        headings: list[_Heading] = []
        fence_char: str | None = None
        fence_length = 0

        for index, raw_line in enumerate(lines):
            line = raw_line.removesuffix("\n")
            fence = _FENCE.match(line)
            if fence_char is not None:
                if (
                    fence is not None
                    and fence.group("marker").startswith(fence_char)
                    and len(fence.group("marker")) >= fence_length
                    and not fence.group("rest").strip()
                ):
                    fence_char = None
                    fence_length = 0
                continue
            if fence is not None:
                marker = fence.group("marker")
                fence_char = marker[0]
                fence_length = len(marker)
                continue

            atx = _ATX_HEADING.match(line)
            if atx is not None:
                title = _ATX_CLOSING.sub("", atx.group("title") or "").strip()
                headings.append(
                    _Heading(starts[index], len(atx.group("marks")), title)
                )
                continue

            if index + 1 >= len(lines) or not line.strip():
                continue
            underline = _SETEXT_UNDERLINE.match(
                lines[index + 1].removesuffix("\n")
            )
            if underline is not None:
                headings.append(
                    _Heading(
                        starts[index],
                        1 if underline.group("marker")[0] == "=" else 2,
                        line.strip(),
                    )
                )

        return headings


__all__ = ["MarkdownStructureAnalyzer"]
