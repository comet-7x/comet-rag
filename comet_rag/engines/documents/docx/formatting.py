from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from docx.document import Document as DocumentObject
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.styles.style import BaseStyle
from docx.text.paragraph import Paragraph
from docx.text.run import Run

type _Attribute = Literal["bold", "italic", "underline", "strike"]


@dataclass(frozen=True, slots=True)
class RunFormat:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False


class RunFormatResolver:
    """一次解析内复用样式查找；文档变更后必须创建新实例。"""

    def __init__(self, document: DocumentObject) -> None:
        self._document = document
        self._styles: dict[tuple[str | None, WD_STYLE_TYPE], BaseStyle | None] = {}
        self._inherited: dict[tuple[BaseOxmlElement, _Attribute], bool | None] = {}

    def resolve(self, run: Run, paragraph: Paragraph) -> RunFormat:
        def resolve_attribute(attr: _Attribute) -> bool:
            direct = getattr(run.font, attr)
            if direct is not None:
                return bool(direct)

            # 保持逐属性短路：直接格式可能遮住样式中未使用的非法值。
            character_style = self._get_style(run._r.style, WD_STYLE_TYPE.CHARACTER)
            sid = str(getattr(character_style, "style_id", "") or "").lower()
            name = str(getattr(character_style, "name", "") or "").lower()
            if "hyperlink" not in sid and "hyperlink" not in name:
                value = self._resolve_inherited(character_style, attr)
                if value is not None:
                    return value

            paragraph_style = self._get_style(
                paragraph._p.style, WD_STYLE_TYPE.PARAGRAPH
            )
            value = self._resolve_inherited(paragraph_style, attr)
            return value if value is not None else False

        underline = resolve_attribute("underline")
        rpr = run._element.find(qn("w:rPr"))
        if rpr is not None:
            em = rpr.find(qn("w:em"))
            if em is not None and em.get(qn("w:val"), "") not in ("", "none"):
                underline = True

        return RunFormat(
            bold=resolve_attribute("bold"),
            italic=resolve_attribute("italic"),
            underline=underline,
            strikethrough=resolve_attribute("strike"),
        )

    def _get_style(
        self, style_id: str | None, style_type: WD_STYLE_TYPE
    ) -> BaseStyle | None:
        key = (style_id, style_type)
        if key not in self._styles:
            # 默认、未知 ID 与类型不符的回退语义仍由 python-docx 决定。
            self._styles[key] = self._document.part.get_style(style_id, style_type)
        return self._styles[key]

    def _resolve_inherited(
        self, style: BaseStyle | None, attr: _Attribute
    ) -> bool | None:
        if style is None:
            return None
        key = (style.element, attr)
        if key not in self._inherited:
            self._inherited[key] = self._walk_inheritance(style, attr)
        return self._inherited[key]

    @staticmethod
    def _walk_inheritance(style: BaseStyle | None, attr: _Attribute) -> bool | None:
        while style is not None:
            font = getattr(style, "font", None)
            if font is not None:
                value = getattr(font, attr)
                if value is not None:
                    return bool(value)
            style = getattr(style, "base_style", None)
        # None 必须保留：字符样式未定义时，还要继续尝试段落样式。
        return None
