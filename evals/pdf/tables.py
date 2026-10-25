from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

_FENCE = re.compile(r"^ {0,3}(?P<marks>`{3,}|~{3,})(?P<rest>.*)$")


@dataclass(frozen=True, slots=True)
class TableSpan:
    start: int
    end: int
    rows: tuple[tuple[int, int], ...]


def _without_fences(text: str) -> str:
    """只屏蔽代码围栏，保留每个字符的位置；不把代码示例当作原生表格。"""
    result = []
    active: str | None = None
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line.rstrip("\n"))
        masked = active is not None or match is not None
        if match:
            marks = match["marks"]
            if active is None:
                active = marks
            elif (
                marks[0] == active[0]
                and len(marks) >= len(active)
                and not match["rest"].strip()
            ):
                active = None
        result.append(
            "".join("\n" if char == "\n" else " " for char in line) if masked else line
        )
    return "".join(result)


class _TableParser(HTMLParser):
    """只记录独立 HTML table/tr 的词法区间，不重写或声称验证 HTML 表格语义。"""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.starts = [0]
        self.starts.extend(match.end() for match in re.finditer("\n", source))
        self.table: int | None = None
        self.row: int | None = None
        self.rows: list[tuple[int, int]] = []
        self.tables: list[TableSpan] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        offset = self._offset()
        if tag == "table":
            if self.table is not None:
                raise ValueError("嵌套 HTML table 暂不纳入完整性审计")
            line, column = self.getpos()
            if column > 3 or self.source[self.starts[line - 1] : offset].strip():
                return
            self.table = offset
            self.rows = []
        elif tag == "tr" and self.table is not None:
            if self.row is not None:
                raise ValueError("HTML table 的行没有显式闭合")
            self.row = offset

    def handle_endtag(self, tag: str) -> None:
        if self.table is None:
            return
        end = self.source.index(">", self._offset()) + 1
        if tag == "tr":
            if self.row is None:
                raise ValueError("HTML table 的行闭合顺序不合法")
            self.rows.append((self.row, end))
            self.row = None
        elif tag == "table":
            if self.row is not None or not self.rows:
                raise ValueError("HTML table 缺少完整显式行")
            self.tables.append(TableSpan(self.table, end, tuple(self.rows)))
            self.table = None


def tables(text: str) -> tuple[TableSpan, ...]:
    """只支持行首至多三个空格的原生 HTML 表格；区间始终引用传入正文。"""
    parser = _TableParser(text)
    parser.feed(_without_fences(text))
    parser.close()
    if parser.table is not None:
        raise ValueError("HTML table 没有显式闭合")
    return tuple(parser.tables)
