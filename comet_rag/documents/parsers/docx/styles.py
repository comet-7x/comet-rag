from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from lxml import etree

from comet_rag.documents.schemas import TextStyle

from .package import Package, R, integer, q, val


def on_off(element: etree._Element) -> bool:
    value = val(element, "true")
    if value not in {"true", "false", "1", "0", "on", "off"}:
        raise ValueError(f"invalid DOCX boolean: {value}")
    return value in {"true", "1", "on"}


class Styles:
    def __init__(self, package: Package) -> None:
        self.styles: dict[str, etree._Element] = {}
        self.default: str | None = None
        self.defaults: etree._Element | None = None
        self._chains: dict[str, tuple[etree._Element, ...]] = {}
        for relation in package.relationships(package.main).values():
            if relation.kind != f"{R}/styles":
                continue
            if relation.external:
                raise ValueError("external DOCX styles are unsupported")
            root = package.xml(relation.target, q("styles"))
            self.defaults = root.find(
                f"{q('docDefaults')}/{q('rPrDefault')}/{q('rPr')}"
            )
            for style in root.findall(q("style")):
                sid = style.get(q("styleId"))
                if not sid:
                    raise ValueError("missing or duplicate DOCX style id")
                if sid in self.styles:
                    raise ValueError("missing or duplicate DOCX style id")
                self.styles[sid] = style
                if style.get(q("type")) == "paragraph" and style.get(q("default")) in {
                    "1",
                    "true",
                    "on",
                }:
                    self.default = sid

    def chain(self, sid: str | None) -> tuple[etree._Element, ...]:
        if sid is None:
            return ()
        if sid not in self._chains:
            chain: list[etree._Element] = []
            seen: set[str] = set()
            cursor: str | None = sid
            while cursor is not None:
                if cursor in seen:
                    raise ValueError("cyclic DOCX style inheritance")
                if len(seen) >= 64:
                    raise ValueError("DOCX style inheritance exceeds 64 levels")
                seen.add(cursor)
                style = self.styles.get(cursor)
                if style is None:
                    break
                chain.append(style)
                cursor = val(style.find(q("basedOn")))
            self._chains[sid] = tuple(reversed(chain))
        return self._chains[sid]

    def paragraph(self, element: etree._Element) -> tuple[etree._Element, ...]:
        sid = val(element.find(f"{q('pPr')}/{q('pStyle')}"), self.default)
        return self.chain(sid)

    def property(self, element: etree._Element, path: str) -> etree._Element | None:
        for owner in (element, *reversed(self.paragraph(element))):
            result = owner.find(f"{q('pPr')}/{path}")
            if result is not None:
                return result
        return None

    def names(self, styles: tuple[etree._Element, ...]) -> tuple[str, ...]:
        return tuple(
            value
            for style in styles
            for value in (style.get(q("styleId")), val(style.find(q("name"))))
            if value
        )

    def heading(self, element: etree._Element) -> int | None:
        outline = self.property(element, q("outlineLvl"))
        if outline is not None:
            level = integer(val(outline), "outline level")
            if level > 9:
                raise ValueError("DOCX outline level exceeds 9")
            return level + 1 if level < 9 else None
        for name in reversed(self.names(self.paragraph(element))):
            match = re.fullmatch(r"heading\s*([1-9])", name, re.IGNORECASE)
            if match:
                return int(match[1])
            if name.lower() == "title":
                return 1
        return None

    def is_code(
        self, element: etree._Element, run: etree._Element | None = None
    ) -> bool:
        styles = self.paragraph(element)
        if run is not None:
            styles += self.chain(val(run.find(f"{q('rPr')}/{q('rStyle')}")))
        return any(
            re.fullmatch(
                r"(?:source\s*)?code(?:\s*(?:block|char))?", name, re.IGNORECASE
            )
            for name in self.names(styles)
        )

    def run_style(self, paragraph: etree._Element, run: etree._Element) -> TextStyle:
        character = self.chain(val(run.find(f"{q('rPr')}/{q('rStyle')}")))
        owners = self.paragraph(paragraph) + character
        properties = (
            [(self.defaults, False)]
            + [(style.find(q("rPr")), True) for style in owners]
            + [(run.find(q("rPr")), False)]
        )
        flags: dict[str, bool | None] = {
            "bold": None,
            "italic": None,
            "underline": None,
            "strike": None,
        }
        font = color = None
        script: Literal["normal", "superscript", "subscript"] | None = None
        size = None
        for rpr, toggle in properties:
            if rpr is None:
                continue
            for xml, name in (
                ("b", "bold"),
                ("i", "italic"),
                ("strike", "strike"),
                ("dstrike", "strike"),
            ):
                node = rpr.find(q(xml))
                if node is not None:
                    value = on_off(node)
                    if toggle:
                        if value:
                            flags[name] = not bool(flags[name])
                    else:
                        flags[name] = value
            underline = rpr.find(q("u"))
            if underline is not None:
                flags["underline"] = val(underline, "single") not in {
                    "none",
                    "0",
                    "false",
                    "off",
                }
            fonts = rpr.find(q("rFonts"))
            if fonts is not None:
                font = (
                    fonts.get(q("ascii"))
                    or fonts.get(q("hAnsi"))
                    or fonts.get(q("eastAsia"))
                    or font
                )
            sz = rpr.find(q("sz"))
            if sz is not None:
                size = integer(val(sz), "font size", minimum=1) / 2
            rgb = rpr.find(q("color"))
            if rgb is not None:
                value = val(rgb)
                if value == "auto":
                    color = None
                elif value is None:
                    raise ValueError("invalid DOCX text color")
                elif re.fullmatch(r"[a-fA-F0-9]{6}", value) is None:
                    raise ValueError("invalid DOCX text color")
                else:
                    color = f"#{value}"
            vertical = rpr.find(q("vertAlign"))
            if vertical is not None:
                value = val(vertical)
                if value not in {"baseline", "superscript", "subscript"}:
                    raise ValueError("invalid DOCX vertical text alignment")
                if value == "superscript":
                    script = "superscript"
                elif value == "subscript":
                    script = "subscript"
                else:
                    script = "normal"
        return TextStyle(
            font=font,
            size_pt=size,
            bold=flags["bold"],
            italic=flags["italic"],
            underline=flags["underline"],
            strike=flags["strike"],
            color=color,
            script=script,
        )


class Numbering:
    def __init__(self, package: Package) -> None:
        self.levels: dict[tuple[int, int], etree._Element] = {}
        self.starts: dict[tuple[int, int], int] = {}
        self.counters: dict[tuple[int, int], int] = {}
        for relation in package.relationships(package.main).values():
            if relation.kind != f"{R}/numbering":
                continue
            if relation.external:
                raise ValueError("external DOCX numbering is unsupported")
            root = package.xml(relation.target, q("numbering"))
            abstract: dict[int, dict[int, etree._Element]] = {}
            for node in root.findall(q("abstractNum")):
                aid = integer(node.get(q("abstractNumId")), "abstract numbering id")
                if aid in abstract:
                    raise ValueError("duplicate DOCX abstract numbering id")
                levels: dict[int, etree._Element] = {}
                for level in node.findall(q("lvl")):
                    index = integer(level.get(q("ilvl")), "numbering level")
                    if index > 8:
                        raise ValueError("invalid DOCX numbering level")
                    if index in levels:
                        raise ValueError("invalid DOCX numbering level")
                    levels[index] = level
                abstract[aid] = levels
            seen: set[int] = set()
            for node in root.findall(q("num")):
                nid = integer(node.get(q("numId")), "numbering id")
                aid = integer(
                    val(node.find(q("abstractNumId"))), "abstract numbering id"
                )
                if nid in seen:
                    raise ValueError("invalid DOCX numbering reference")
                if aid not in abstract:
                    raise ValueError("invalid DOCX numbering reference")
                seen.add(nid)
                self.levels.update(
                    {(nid, index): level for index, level in abstract[aid].items()}
                )
                for override in node.findall(q("lvlOverride")):
                    index = integer(override.get(q("ilvl")), "numbering override level")
                    if index > 8:
                        raise ValueError("invalid DOCX numbering level")
                    level = override.find(q("lvl"))
                    if level is not None:
                        self.levels[nid, index] = level
                    start = override.find(q("startOverride"))
                    if start is not None:
                        self.starts[nid, index] = integer(val(start), "numbering start")

    def marker(
        self, nid: int, level: int, issue: Callable[[str, str], None]
    ) -> tuple[str | None, dict[str, object]]:
        node = self.levels.get((nid, level))
        if node is None:
            raise ValueError("missing DOCX list numbering level")
        fmt = val(node.find(q("numFmt")), "decimal")
        template = val(node.find(q("lvlText")), "") or ""
        start = self.starts.get(
            (nid, level), integer(val(node.find(q("start")), "1"), "numbering start")
        )
        key = (nid, level)
        self.counters[key] = self.counters.get(key, start - 1) + 1
        for nested in range(level + 1, 9):
            nested_node = self.levels.get((nid, nested))
            if nested_node is not None:
                restart = val(nested_node.find(q("lvlRestart")))
                if (
                    restart is None
                    or integer(restart, "numbering restart") == level + 1
                ):
                    self.counters.pop((nid, nested), None)
        layout: dict[str, object] = {
            "num_id": nid,
            "numbering_format": fmt,
            "marker_template": template,
        }
        indent = node.find(f"{q('pPr')}/{q('ind')}")
        if indent is not None:
            layout["indent_twips"] = {
                etree.QName(key).localname: value
                for key, value in indent.attrib.items()
            }
        font = node.find(f"{q('rPr')}/{q('rFonts')}")
        if font is not None:
            layout["marker_fonts"] = {
                etree.QName(key).localname: value for key, value in font.attrib.items()
            }
        if fmt in {"bullet", "none"}:
            return template, layout
        if (
            fmt
            not in {
                "decimal",
                "decimalZero",
                "lowerLetter",
                "upperLetter",
                "lowerRoman",
                "upperRoman",
            }
            or node.find(q("isLgl")) is not None
        ):
            issue(
                "unsupported_numbering",
                f"List marker format {fmt!r} is preserved as a template.",
            )
            return None, layout

        def substitute(match: re.Match[str]) -> str:
            index = int(match[1]) - 1
            ancestor = self.levels.get((nid, index))
            if ancestor is None:
                raise ValueError("DOCX list marker refers to a missing level")
            value = self.counters.get(
                (nid, index),
                self.starts.get(
                    (nid, index),
                    integer(val(ancestor.find(q("start")), "1"), "numbering start"),
                ),
            )
            ancestor_fmt = val(ancestor.find(q("numFmt")), "decimal")
            if ancestor_fmt == "decimalZero":
                return f"{value:02d}"
            if ancestor_fmt in {"lowerLetter", "upperLetter"} and value > 0:
                letters = ""
                while value:
                    value, remainder = divmod(value - 1, 26)
                    letters = chr(97 + remainder) + letters
                return letters.upper() if ancestor_fmt == "upperLetter" else letters
            if ancestor_fmt in {"lowerRoman", "upperRoman"}:
                if not 0 < value < 4000:
                    issue(
                        "unsupported_numbering",
                        "Roman list marker is outside the supported range.",
                    )
                    return str(value)
                roman = ""
                for number, glyph in (
                    (1000, "M"),
                    (900, "CM"),
                    (500, "D"),
                    (400, "CD"),
                    (100, "C"),
                    (90, "XC"),
                    (50, "L"),
                    (40, "XL"),
                    (10, "X"),
                    (9, "IX"),
                    (5, "V"),
                    (4, "IV"),
                    (1, "I"),
                ):
                    count, value = divmod(value, number)
                    roman += glyph * count
                return roman.lower() if ancestor_fmt == "lowerRoman" else roman
            return str(value)

        return re.sub(r"%([1-9])", substitute, template), layout
